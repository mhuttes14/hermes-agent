/**
 * Durable "we attempted an update" receipt.
 *
 * The staged updater is spawned `detached` with `stdio: 'ignore'` and the
 * desktop calls `app.quit()` ~2.5s later (see `applyUpdates` in main.ts), so
 * Electron can never observe the updater's exit code. That is not an oversight
 * we can fix by waiting: the updater force-kills surviving Hermes processes,
 * replaces the very `.app` bundle we are executing from, and relaunches us at
 * the end. Waiting is actively defended against.
 *
 * The consequence was a silent loop. When `hermes update` failed (e.g. a
 * committed local patch that no longer rebases cleanly), nothing recorded the
 * failure. On the next launch the poller saw `behind > 0` again and re-fired
 * the cheerful "update available" toast — walking the user straight back into
 * the identical failure, with no error surfaced anywhere in the desktop app.
 *
 * So instead of observing the child, we record what we *attempted* and
 * reconcile it against git HEAD on the next check. If HEAD never moved, the
 * update did not land.
 *
 * This module holds the PURE, side-effect-light logic (path, parse, staleness,
 * reconcile) so it is unit-testable without booting Electron — same split as
 * update-marker.ts.
 */

import fs from 'fs'
import path from 'path'

// A receipt older than this is self-healed away. It only exists to answer "did
// the update we just launched land?", so anything this old is a leftover from
// a machine that was shut down mid-update, not a signal worth acting on.
export const RECEIPT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000

export function receiptPath(hermesHome) {
  // Beside .hermes-update-in-progress, directly under HERMES_HOME, so it
  // survives the checkout being deleted and recreated by a repair install.
  return path.join(hermesHome, '.hermes-update-receipt.json')
}

/**
 * Record an update attempt, immediately after spawning the updater.
 *
 * `currentSha` is the HEAD we are leaving FROM — that is the field reconcile
 * compares against, because "did HEAD move at all?" is the only question we can
 * answer reliably. Best-effort: a failure to write must never block the update.
 */
export function writeUpdateAttempt(
  hermesHome,
  { branch, currentSha, targetSha },
  { now = Date.now } = {}
) {
  try {
    fs.writeFileSync(
      receiptPath(hermesHome),
      JSON.stringify({ attemptedAt: now(), branch, currentSha, targetSha }),
      'utf8'
    )
  } catch {
    // Best-effort: proceed with the update regardless.
  }
}

export function clearUpdateReceipt(hermesHome) {
  try {
    fs.unlinkSync(receiptPath(hermesHome))
  } catch {
    void 0
  }
}

/**
 * Decide whether the last recorded attempt actually landed.
 *
 * Returns the receipt when the attempt provably did NOT land (so callers can
 * surface a persistent failure), and `null` in every other case.
 *
 * Compares against the recorded `currentSha` ("did we move at all?") rather
 * than `targetSha` ("did we reach the exact tip we saw?"). Upstream advances
 * constantly — often hundreds of commits a day — so a perfectly successful
 * update routinely lands on a newer sha than the one that was checked, and
 * comparing against `targetSha` would report false failures.
 */
export function reconcileUpdateReceipt(
  hermesHome,
  {
    currentSha,
    now = Date.now,
    maxAgeMs = RECEIPT_MAX_AGE_MS
  }: { currentSha?: string; now?: () => number; maxAgeMs?: number } = {}
) {
  const file = receiptPath(hermesHome)
  let receipt

  try {
    receipt = JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    // Absent is the common case. Malformed is a corrupt leftover — drop it so
    // it cannot wedge the check forever.
    clearUpdateReceipt(hermesHome)

    return null
  }

  if (!receipt || typeof receipt !== 'object') {
    clearUpdateReceipt(hermesHome)

    return null
  }

  const attemptedAt = Number(receipt.attemptedAt)

  if (!Number.isFinite(attemptedAt) || now() - attemptedAt > maxAgeMs) {
    clearUpdateReceipt(hermesHome)

    return null
  }

  // Undecidable: we could not read HEAD this time round. Keep the receipt and
  // try again on the next check rather than guessing either way.
  if (!currentSha) {
    return receipt
  }

  if (currentSha !== receipt.currentSha) {
    clearUpdateReceipt(hermesHome) // HEAD moved => the update landed.

    return null
  }

  return receipt
}
