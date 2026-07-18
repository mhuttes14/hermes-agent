"""Treehouse pooled-worktree backend for Hermes kanban workspaces.

This module is the *only* place that knows how to talk to the ``treehouse`` CLI.
``kanban_db.py`` calls into it at three seams (acquire on resolve, return on
completion, reconcile on the dispatch loop) so the treehouse-specific logic stays
isolated and the core's diff stays tiny.

Model (verified against treehouse v1.4.0):
  * Pools are PER-REPO under ``~/.treehouse/<repo>-<hash>/``; ``treehouse get``
    holds a tree only while a subshell lives and auto-returns on exit.
  * We acquire by launching a detached holder — ``treehouse get`` fed
    ``echo $TREEHOUSE_DIR; exec sleep <forever>`` — which prints the worktree path
    then becomes a long-lived process keeping the tree ``in-use``. The kanban
    worker then runs with cwd inside that tree (a second holder), so even if the
    worker crashes the tree stays held until we release it.
  * We release with ``treehouse return --force <path>`` (kills every process whose
    cwd is in the tree — holder + worker — resets, frees the slot). Idempotent.

Lease ledger: each acquire writes ``<leases>/<task_id>.json`` = {repo, path,
holder_pid}. The reconciler reclaims trees for tasks that ended without normal
cleanup, gating every reclaim on the holder pid still being ALIVE — because pool
paths are reused, releasing a tree by a stale path could reclaim a *live* task's
tree. While the holder is alive, the path provably still belongs to that task.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

SENTINEL_PREFIX = "treehouse:"
_TREEHOUSE_DIR_MARK = f"{os.sep}.treehouse{os.sep}"

# Task statuses for which a tree should be reclaimed if still held. A crashed
# worker resets its task to 'ready' (it will be redispatched and reuse the same
# still-held tree), so 'ready'/'running' are NOT reclaimed.
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "archived"})


def _treehouse_bin() -> str:
    return os.environ.get("TREEHOUSE_BIN") or os.path.expanduser("~/go/bin/treehouse")


def is_treehouse_spec(spec: Optional[str]) -> bool:
    """True if a workspace_path is the acquire sentinel ``treehouse:<repo>``."""
    return bool(spec) and spec.startswith(SENTINEL_PREFIX)


def is_treehouse_path(path: Optional[str]) -> bool:
    """True if a path is a treehouse pool worktree (durable post-acquire signal)."""
    return bool(path) and _TREEHOUSE_DIR_MARK in str(path)


def _leases_dir() -> Path:
    override = os.environ.get("HERMES_TREEHOUSE_LEASES", "").strip()
    if override:
        d = Path(override).expanduser()
    else:
        # Lazy import to avoid a circular import at module load.
        try:
            from .kanban_db import kanban_home

            d = kanban_home() / "kanban" / "treehouse-leases"
        except Exception:
            d = Path("~/.hermes/kanban/treehouse-leases").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lease_file(task_id: str) -> Path:
    return _leases_dir() / f"{task_id}.json"


def _read_lease(task_id: str) -> Optional[dict]:
    try:
        return json.loads(_lease_file(task_id).read_text())
    except Exception:
        return None


def _write_lease(task_id: str, data: dict) -> None:
    try:
        _lease_file(task_id).write_text(json.dumps(data))
    except Exception:
        pass


def _remove_lease(task_id: str) -> None:
    try:
        _lease_file(task_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # exists, owned by another user


def _acquire_fresh(repo: str, task_id: str) -> str:
    """Acquire a fresh pooled worktree for *repo*, record a lease, return its path."""
    repo_abs = os.path.abspath(os.path.expanduser(repo))
    if not os.path.isdir(repo_abs):
        raise ValueError(f"treehouse repo is not a directory: {repo_abs}")
    th = _treehouse_bin()
    # Detached holder: prints $TREEHOUSE_DIR then becomes a long-lived sleep that
    # keeps the tree in-use. start_new_session detaches it from the dispatcher.
    #
    # The holder's stdout MUST go to a temp file, not a pipe: the exec'd sleep
    # inherits stdout and holds it open forever, so a PIPE would never close and
    # the dispatcher would appear to hang on it.
    import tempfile

    # Single-quote the printf format so the OUTER /bin/sh does NOT expand
    # $TREEHOUSE_DIR (it is unset there) — only the innermost treehouse subshell,
    # where treehouse sets it, should expand it.
    inner = (
        "printf 'echo $TREEHOUSE_DIR; exec sleep 2147483647\\n' | " + repr(th) + " get"
    )
    out_fd, out_path = tempfile.mkstemp(prefix="th-acq-")
    path = ""
    try:
        with os.fdopen(out_fd, "wb") as out_f:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", inner],
                cwd=repo_abs,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                text = Path(out_path).read_text("utf-8", "replace")
            except Exception:
                text = ""
            for ln in text.splitlines():
                ln = ln.strip()
                if ln.startswith("/") and _TREEHOUSE_DIR_MARK in ln:
                    path = ln
                    break
            if path:
                break
            time.sleep(0.1)
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass
    if not path:
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError(
            f"treehouse acquire failed for {repo_abs} (pool exhausted or treehouse error)"
        )
    _write_lease(
        task_id,
        {
            "repo": repo_abs,
            "path": path,
            "holder_pid": proc.pid,
            "ts": int(time.time()),
        },
    )
    return path


def _return_path(path: str) -> None:
    """Return a pool worktree (kills holder + worker, resets, frees). Idempotent."""
    if not path:
        return
    th = _treehouse_bin()
    # Run from the worktree's MAIN repo, never inside the tree (treehouse kills
    # processes whose cwd is in the tree — including ours).
    cwd = None
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                path,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if common.endswith(os.sep + ".git"):
            main = common[: -len(os.sep + ".git")]
            if os.path.isdir(main):
                cwd = main
    except Exception:
        cwd = None
    try:
        subprocess.run(
            [th, "return", "--force", path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass


# --- The three seams kanban_db.py calls -----------------------------------


def resolve_for_task(task_id: str, spec: str) -> str:
    """Return the pool worktree path a task should use.

    *spec* is either the ``treehouse:<repo>`` sentinel (first dispatch) or a
    concrete pool path that was persisted on a prior dispatch (rerun after a
    crash). Reuses the still-held tree when valid; re-acquires fresh otherwise.
    """
    lease = _read_lease(task_id)
    if is_treehouse_spec(spec):
        repo = spec[len(SENTINEL_PREFIX) :]
        if (
            lease
            and _pid_alive(lease.get("holder_pid"))
            and os.path.isdir(lease.get("path", ""))
        ):
            return lease["path"]
        return _acquire_fresh(repo, task_id)
    # Concrete pool path (rerun). Reuse only if we still provably hold it.
    if lease and _pid_alive(lease.get("holder_pid")) and os.path.isdir(spec):
        return spec
    repo = (lease or {}).get("repo") or _derive_repo(spec)
    if repo:
        return _acquire_fresh(repo, task_id)
    return spec  # best effort — let the worktree branch return it as-is


def release_for_task(task_id: str, path: Optional[str] = None) -> None:
    """Return a task's pooled worktree on normal completion and drop its lease."""
    lease = _read_lease(task_id)
    target = path or (lease or {}).get("path")
    if target:
        _return_path(target)
    _remove_lease(task_id)


def reconcile(status_of: Callable[[str], Optional[str]]) -> list[str]:
    """Reclaim pooled worktrees whose owning task ended without normal cleanup.

    *status_of(task_id)* returns the task's current status, or None if it no
    longer exists. Every reclaim is gated on the holder pid still being alive,
    so a reused pool path can never reclaim a live task's tree. Returns the list
    of task ids reclaimed.
    """
    reclaimed: list[str] = []
    try:
        files = list(_leases_dir().glob("*.json"))
    except Exception:
        return reclaimed
    for lf in files:
        task_id = lf.stem
        lease = _read_lease(task_id)
        if not lease:
            try:
                lf.unlink()
            except Exception:
                pass
            continue
        status = status_of(task_id)
        alive = _pid_alive(lease.get("holder_pid"))
        if alive:
            # Holder alive => the tree provably still belongs to this task.
            if status is None or status in TERMINAL_STATUSES:
                _return_path(lease.get("path", ""))
                _remove_lease(task_id)
                reclaimed.append(task_id)
            # else running/ready => legitimately held; leave it.
        else:
            # Holder dead: the tree is already free or reused by another task.
            # Don't release by (possibly stale) path; just drop stale bookkeeping
            # once the task is no longer running.
            if status is None or status in TERMINAL_STATUSES:
                _remove_lease(task_id)
    return reclaimed


def _derive_repo(worktree_path: str) -> Optional[str]:
    try:
        common = subprocess.run(
            [
                "git",
                "-C",
                worktree_path,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if common.endswith(os.sep + ".git"):
            main = common[: -len(os.sep + ".git")]
            if os.path.isdir(main):
                return main
    except Exception:
        return None
    return None
