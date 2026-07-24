/**
 * Tests for electron/update-receipt.ts — the durable record of an attempted
 * update, reconciled against git HEAD on the next check.
 *
 * Why this matters: the staged updater is spawned detached with stdio ignored
 * and the desktop quits before it even starts, so a failed `hermes update` was
 * completely invisible to the app. The poller then saw `behind > 0` again and
 * re-offered the identical doomed update on every relaunch. Reconciling the
 * receipt is what converts that silent loop into a visible, actionable state.
 *
 * The load-bearing rule: compare against the recorded `currentSha` ("did HEAD
 * move at all?"), never `targetSha` — upstream moves hundreds of commits a day,
 * so a successful update routinely lands past the sha that was checked.
 */

import fs from 'fs'
import assert from 'node:assert/strict'
import os from 'os'
import path from 'path'

import { test } from 'vitest'

import {
  clearUpdateReceipt,
  RECEIPT_MAX_AGE_MS,
  receiptPath,
  reconcileUpdateReceipt,
  writeUpdateAttempt
} from './update-receipt'

function tmpHome(tag) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `hermes-receipt-${tag}-`))
}

const ATTEMPT = { branch: 'main', currentSha: 'aaa111', targetSha: 'bbb222' }

test('absent receipt => nothing to report', () => {
  const home = tmpHome('absent')

  assert.equal(reconcileUpdateReceipt(home, { currentSha: 'aaa111' }), null)
})

test('HEAD moved => update landed; receipt is cleared', () => {
  const home = tmpHome('landed')

  writeUpdateAttempt(home, ATTEMPT)

  // Note ccc333 is NOT the targetSha we recorded — upstream moved on. This is
  // the normal success case and must NOT be reported as a failure.
  assert.equal(reconcileUpdateReceipt(home, { currentSha: 'ccc333' }), null)
  assert.equal(fs.existsSync(receiptPath(home)), false)
})

test('HEAD unchanged => the update did not land; receipt is returned and kept', () => {
  const home = tmpHome('failed')

  writeUpdateAttempt(home, ATTEMPT)

  const failure = reconcileUpdateReceipt(home, { currentSha: 'aaa111' })

  assert.equal(failure.branch, 'main')
  assert.equal(failure.targetSha, 'bbb222')
  assert.equal(fs.existsSync(receiptPath(home)), true)
})

test('expired receipt self-heals', () => {
  const home = tmpHome('expired')

  writeUpdateAttempt(home, ATTEMPT, { now: () => 1_000 })

  const later = 1_000 + RECEIPT_MAX_AGE_MS + 1

  assert.equal(reconcileUpdateReceipt(home, { currentSha: 'aaa111', now: () => later }), null)
  assert.equal(fs.existsSync(receiptPath(home)), false)
})

test('unknown HEAD is undecidable => keep the receipt for the next check', () => {
  const home = tmpHome('undecidable')

  writeUpdateAttempt(home, ATTEMPT)

  assert.notEqual(reconcileUpdateReceipt(home, {}), null)
  assert.equal(fs.existsSync(receiptPath(home)), true)
})

test('malformed receipt self-heals', () => {
  const home = tmpHome('malformed')

  fs.writeFileSync(receiptPath(home), '{not json', 'utf8')

  assert.equal(reconcileUpdateReceipt(home, { currentSha: 'aaa111' }), null)
  assert.equal(fs.existsSync(receiptPath(home)), false)
})

test('write/clear round-trip', () => {
  const home = tmpHome('roundtrip')

  writeUpdateAttempt(home, ATTEMPT)
  assert.equal(fs.existsSync(receiptPath(home)), true)

  clearUpdateReceipt(home)
  assert.equal(fs.existsSync(receiptPath(home)), false)
  // Clearing twice must not throw.
  clearUpdateReceipt(home)
})
