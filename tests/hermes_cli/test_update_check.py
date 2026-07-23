"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()




@pytest.mark.parametrize(
    ("upstream_sha", "ancestor_exit", "expected"),
    [
        pytest.param("local-sha", 0, 0, id="equal"),
        pytest.param("upstream-ancestor", 0, 0, id="upstream-is-ancestor"),
        pytest.param("new-upstream", 1, 1, id="genuinely-behind"),
        pytest.param("diverged-upstream", 1, 1, id="diverged"),
        pytest.param("missing-object", 128, 1, id="upstream-object-missing"),
        pytest.param("unknown-failure", 2, None, id="unexpected-git-failure"),
    ],
)
def test_check_for_updates_official_ssh_origin_uses_https_ancestor_probe(
    tmp_path, upstream_sha, ancestor_exit, expected
):
    """Official SSH checks avoid auth and tolerate commits on top of upstream."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return MagicMock(returncode=0, stdout="git@github.com:NousResearch/hermes-agent.git\n")
        if cmd == [
            "git",
            "ls-remote",
            "https://github.com/NousResearch/hermes-agent.git",
            "refs/heads/main",
        ]:
            return MagicMock(returncode=0, stdout=f"{upstream_sha}\trefs/heads/main\n")
        if cmd == ["git", "merge-base", "--is-ancestor", upstream_sha, "HEAD"]:
            return MagicMock(returncode=ancestor_exit, stdout="")
        raise AssertionError(f"unexpected git command: {cmd!r}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = banner._check_via_local_git(repo_dir)

    assert result == expected
    assert ["git", "fetch", "origin", "--quiet"] not in calls


def test_check_via_rev_keeps_embedded_revision_exact_match_semantics():
    """The checkout-free Nix helper must not start running merge-base."""
    import hermes_cli.banner as banner

    with patch(
        "hermes_cli.banner.subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout="upstream-sha\trefs/heads/main\n",
        ),
    ) as mock_run:
        result = banner._check_via_rev("embedded-sha")

    assert result == banner.UPDATE_AVAILABLE_NO_COUNT
    # Assert the command shape only. The point of this test is that the
    # checkout-free Nix path never shells out to merge-base; pinning the full
    # kwargs makes it fail on unrelated upstream subprocess sweeps (e.g. the
    # utf-8 encoding/errors guard added to every git call in banner.py).
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0] == [
        "git",
        "ls-remote",
        "https://github.com/NousResearch/hermes-agent.git",
        "refs/heads/main",
    ]




def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5




