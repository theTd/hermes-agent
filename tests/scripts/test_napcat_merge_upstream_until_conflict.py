"""Tests for scripts/napcat_merge_upstream_until_conflict.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "napcat_merge_upstream_until_conflict.py"
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in a temporary repository."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with code {result.returncode}: {result.stderr}"
        )
    return result


def run_script(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the merge script inside a temporary repository."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )


def write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    return repo


def clone_repo(remote: Path, destination: Path) -> Path:
    git(destination.parent, "clone", str(remote), destination.name)
    git(destination, "config", "user.email", "test@example.com")
    git(destination, "config", "user.name", "Test User")
    return destination


def test_dry_run_lists_upstream_steps_and_local_replay(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    write_file(repo / "note.txt", "base\n")
    commit_all(repo, "base")

    git(repo, "checkout", "-b", "feature")
    write_file(repo / "feature.txt", "feature change\n")
    commit_all(repo, "feature change")

    git(repo, "checkout", "main")
    write_file(repo / "note.txt", "upstream change 1\n")
    commit_all(repo, "upstream change 1")
    write_file(repo / "other.txt", "upstream change 2\n")
    commit_all(repo, "upstream change 2")
    git(repo, "checkout", "feature")

    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    result = run_script(repo, "main", "--dry-run")
    after = git(repo, "rev-parse", "HEAD").stdout.strip()

    assert result.returncode == 0
    assert before == after
    assert "Dry run only." in result.stdout
    assert "Upstream first-parent commits to advance through: 2" in result.stdout
    assert "upstream change 1" in result.stdout
    assert "upstream change 2" in result.stdout
    assert "feature change" in result.stdout


def test_stops_on_second_upstream_commit_conflict(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    write_file(repo / "shared.txt", "base\n")
    commit_all(repo, "base")

    git(repo, "checkout", "-b", "feature")
    write_file(repo / "shared.txt", "feature branch change\n")
    commit_all(repo, "feature change")

    git(repo, "checkout", "main")
    write_file(repo / "other.txt", "first upstream change\n")
    commit_all(repo, "upstream step 1")
    write_file(repo / "shared.txt", "upstream conflicting change\n")
    commit_all(repo, "upstream step 2")

    git(repo, "checkout", "feature")
    result = run_script(repo, "main")

    assert result.returncode == 1
    assert "[1/2] Rebasing onto upstream commit: " in result.stdout
    assert "upstream step 1" in result.stdout
    assert "[2/2] Rebasing onto upstream commit: " in result.stdout
    assert "upstream step 2" in result.stdout
    assert "Conflict encountered while advancing onto upstream commit" in result.stderr
    assert (repo / "other.txt").exists()

    status = git(repo, "status", "--porcelain").stdout
    assert "UU shared.txt" in status

    rebase_head = git(repo, "rev-parse", "-q", "--verify", "REBASE_HEAD", check=False)
    assert rebase_head.returncode == 0


def test_updates_local_main_before_rebasing(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", remote.name)

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test User")
    write_file(seed / "base.txt", "base\n")
    commit_all(seed, "base")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "--git-dir", remote.name, "symbolic-ref", "HEAD", "refs/heads/main")

    repo = clone_repo(remote, tmp_path / "repo")
    publisher = clone_repo(remote, tmp_path / "publisher")

    git(repo, "checkout", "-b", "feature")
    write_file(repo / "feature.txt", "feature change\n")
    commit_all(repo, "feature change")

    git(publisher, "checkout", "main")
    write_file(publisher / "base.txt", "upstream change\n")
    commit_all(publisher, "upstream change")
    git(publisher, "push", "origin", "main")

    git(repo, "checkout", "main")
    git(repo, "fetch", "origin")
    local_main_before = git(repo, "rev-parse", "main").stdout.strip()
    origin_main_before = git(repo, "rev-parse", "origin/main").stdout.strip()
    assert local_main_before != origin_main_before

    git(repo, "checkout", "feature")
    result = run_script(repo, "main")

    assert result.returncode == 0
    assert "Fetching origin for local source branch main..." in result.stdout
    assert "Fast-forwarding local main to origin/main..." in result.stdout

    local_main_after = git(repo, "rev-parse", "main").stdout.strip()
    origin_main_after = git(repo, "rev-parse", "origin/main").stdout.strip()
    assert local_main_after == origin_main_after
    assert local_main_after != local_main_before

    ancestor = git(repo, "merge-base", "--is-ancestor", "main", "HEAD", check=False)
    assert ancestor.returncode == 0

    head_message = git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert head_message == "feature change"


def test_dry_run_uses_first_parent_mainline_only(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    write_file(repo / "base.txt", "base\n")
    commit_all(repo, "base")

    git(repo, "checkout", "-b", "feature")
    write_file(repo / "feature.txt", "feature change\n")
    commit_all(repo, "feature change")

    git(repo, "checkout", "main")
    write_file(repo / "mainline.txt", "mainline step 1\n")
    commit_all(repo, "mainline step 1")

    git(repo, "checkout", "-b", "side")
    write_file(repo / "side.txt", "side branch change\n")
    commit_all(repo, "side branch change")

    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "side", "-m", "merge side branch")
    write_file(repo / "mainline.txt", "mainline step 2\n")
    commit_all(repo, "mainline step 2")
    git(repo, "checkout", "feature")

    result = run_script(repo, "main", "--dry-run")

    assert result.returncode == 0
    assert "Upstream first-parent commits to advance through: 3" in result.stdout
    assert "mainline step 1" in result.stdout
    assert "merge side branch" in result.stdout
    assert "mainline step 2" in result.stdout
    assert "side branch change" not in result.stdout
