// Whether `git rev-list HEAD..origin/<branch> --count` produces a meaningful
// number worth computing. On a SHALLOW checkout (installer clones with
// --depth 1) the local history often shares no merge-base with the freshly
// fetched origin tip, so the count enumerates the entire remote ancestry and
// returns a bogus huge number (e.g. 12104) — see #51922. resolveBehindCount
// discards that bogus count in favour of a SHA compare, so the caller should
// SKIP the expensive rev-list entirely in that case rather than run it and
// throw the result away.
function shouldCountCommits({ isShallow, hasMergeBase }) {
  return !(isShallow && !hasMergeBase)
}

// Resolve a presence-only update check when an exact commit count is not
// available. A checkout with local commits is still current when the upstream
// tip is already in its history.
function resolveBinaryBehindCount({ currentSha, targetSha, ancestorExitCode }) {
  if (currentSha && targetSha && (currentSha === targetSha || ancestorExitCode === 0)) {
    return 0
  }

  return 1
}

// Resolve how many commits the local checkout is behind origin for the desktop
// update indicator. When the count isn't meaningful (shallow + no merge-base)
// fall back to a binary up-to-date check. Full clones (developers / Docker dev
// images) keep the exact count path unchanged.
function resolveBehindCount({
  countStr,
  currentSha,
  targetSha,
  ancestorExitCode = undefined,
  isShallow,
  hasMergeBase
}) {
  if (!shouldCountCommits({ isShallow, hasMergeBase })) {
    return resolveBinaryBehindCount({ currentSha, targetSha, ancestorExitCode })
  }

  return Number.parseInt(countStr, 10) || 0
}

export { resolveBehindCount, resolveBinaryBehindCount, shouldCountCommits }
