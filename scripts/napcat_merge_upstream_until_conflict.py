#!/usr/bin/env python3
"""Advance a carried branch through upstream first-parent commits one at a time.

The script first refreshes a local source branch from its upstream, when one is
configured, then walks the source branch's first-parent chain from the current
merge-base to the source HEAD. For each upstream mainline commit, it rebases the
current branch onto that single commit boundary. If a conflict occurs, git stops
on that step and the script exits, leaving the rebase state intact for manual
resolution.

Operational notes from real replay runs:
    1. Always follow upstream with ``git rev-list --first-parent``. A plain
       ``rev-list`` walks the whole reachable DAG, which replays side branches
       and produces a flood of conflicts that looks like a direct rebase to the
       remote HEAD.
    2. Refresh the local source branch before planning steps. If ``main`` tracks
       ``origin/main``, fast-forward local ``main`` first so the replay plan is
       based on the true upstream HEAD rather than a stale local ref.
    3. Treat each upstream mainline commit as one boundary and rebase local
       commits with ``git rebase --onto <upstream-step> <previous-base>``. When
       a step succeeds, rerun the script to continue from the new merge-base.
    4. If a conflict occurs, resolve only the current step, then continue the
       in-progress rebase and rerun the script. On Windows or other environments
       where git may block on an editor during ``git rebase --continue``, use
       ``GIT_EDITOR=true git rebase --continue``.
    5. After resolving a conflict, explicitly check for leftover damage before
       continuing: conflict markers, partially merged logic, wrong manual
       stitch-ups, or any other residue from the previous mistake. Do not carry
       a bad resolution into the next upstream step.
    6. Use ``--dry-run`` before a long replay to confirm the exact first-parent
       plan and the local commits that will be replayed at every step.

Examples:
    python scripts/napcat_merge_upstream_until_conflict.py main
    python scripts/napcat_merge_upstream_until_conflict.py origin/main
    python scripts/napcat_merge_upstream_until_conflict.py origin/main --dry-run
    python scripts/napcat_merge_upstream_until_conflict.py main --rebase-merges
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SHORT_TIMEOUT_SEC = 30
LONG_TIMEOUT_SEC = 300


def _run_post_rebase_sanity_checks(repo_root: Path) -> int:
    """Run automated sanity checks after a successful rebase step.

    Returns the number of warnings emitted.
    """
    warnings = 0

    # Check for leftover conflict markers in Python files
    result = subprocess.run(
        ["grep", "-rlE", "^(<<<<<<<|=======|>>>>>>>)", "--include=*.py", "."],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        if files:
            print("WARNING: Found leftover conflict markers in:", file=sys.stderr)
            for f in files[:10]:
                print(f"  {f}", file=sys.stderr)
            if len(files) > 10:
                print(f"  ... and {len(files) - 10} more", file=sys.stderr)
            warnings += len(files)

    return warnings


def run_git(
    repo_root: Path,
    *args: str,
    check: bool = True,
    timeout: int = SHORT_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in *repo_root*."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def detect_repo_root() -> Path:
    """Return the current git repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SHORT_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError("Not inside a git repository.")
    return Path(result.stdout.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Advance the current branch through upstream first-parent commits "
            "one at a time, stopping at the first conflict."
        )
    )
    parser.add_argument(
        "source",
        help="Source ref to follow, for example main or origin/main.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch the source remote before computing the upstream mainline commit list.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running with a dirty working tree.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the upstream steps and replayed local commits without changing anything.",
    )
    parser.add_argument(
        "--rebase-merges",
        action="store_true",
        help="Preserve local merge commits during each rebase step.",
    )
    return parser.parse_args()


def current_branch(repo_root: Path) -> str:
    """Return the current branch name or HEAD for detached state."""
    result = run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def working_tree_dirty(repo_root: Path) -> bool:
    """Return True when the working tree has staged, unstaged, or untracked changes."""
    result = run_git(repo_root, "status", "--porcelain", check=False)
    return bool(result.stdout.strip())


def maybe_fetch(repo_root: Path, source: str) -> None:
    """Fetch the remote part of *source* when it looks like remote/ref."""
    remote_name, has_separator, _rest = source.partition("/")
    if not has_separator:
        raise RuntimeError(
            "--fetch expects a remote-tracking source like origin/main or upstream/main."
        )
    print(f"Fetching {remote_name}...")
    run_git(repo_root, "fetch", remote_name, timeout=LONG_TIMEOUT_SEC)


def local_branch_exists(repo_root: Path, branch: str) -> bool:
    """Return True if *branch* exists as a local branch."""
    result = run_git(
        repo_root,
        "show-ref",
        "--verify",
        f"refs/heads/{branch}",
        check=False,
    )
    return result.returncode == 0


def branch_upstream(repo_root: Path, branch: str) -> str | None:
    """Return the upstream ref for *branch*, if configured."""
    result = run_git(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        f"{branch}@{{upstream}}",
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_ref(repo_root: Path, ref: str) -> str:
    """Resolve *ref* to a commit hash."""
    result = run_git(repo_root, "rev-parse", ref)
    return result.stdout.strip()


def is_ancestor(repo_root: Path, older: str, newer: str) -> bool:
    """Return True if *older* is an ancestor of *newer*."""
    result = run_git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        older,
        newer,
        check=False,
    )
    return result.returncode == 0


def sync_local_source_branch(repo_root: Path, branch: str, current_branch_name: str) -> None:
    """Fast-forward a local source branch to its upstream before rebasing."""
    if not local_branch_exists(repo_root, branch):
        return

    upstream = branch_upstream(repo_root, branch)
    if not upstream:
        return

    remote_name, has_separator, _rest = upstream.partition("/")
    if not has_separator:
        raise RuntimeError(
            f"Upstream for local branch '{branch}' is '{upstream}', which is not a remote-tracking ref."
        )

    print(f"Fetching {remote_name} for local source branch {branch}...")
    run_git(repo_root, "fetch", remote_name, timeout=LONG_TIMEOUT_SEC)

    local_head = resolve_ref(repo_root, branch)
    upstream_head = resolve_ref(repo_root, upstream)
    if local_head == upstream_head:
        return

    if not is_ancestor(repo_root, local_head, upstream_head):
        raise RuntimeError(
            f"Local source branch '{branch}' has diverged from its upstream '{upstream}'. "
            "Update it manually before rebasing."
        )

    if current_branch_name == branch:
        raise RuntimeError(
            f"Local source branch '{branch}' is currently checked out and needs to be fast-forwarded. "
            "Check out the target branch first, then rerun the script."
        )

    print(f"Fast-forwarding local {branch} to {upstream}...")
    run_git(
        repo_root,
        "update-ref",
        f"refs/heads/{branch}",
        upstream_head,
        local_head,
    )


def merge_base(repo_root: Path, source: str) -> str:
    """Return the merge-base between HEAD and *source*."""
    result = run_git(repo_root, "merge-base", "HEAD", source)
    return result.stdout.strip()


def current_head(repo_root: Path) -> str:
    """Return the current HEAD commit hash."""
    result = run_git(repo_root, "rev-parse", "HEAD")
    return result.stdout.strip()


def local_commit_list(repo_root: Path, source: str) -> list[str]:
    """Return local commits that are not reachable from *source*."""
    result = run_git(repo_root, "rev-list", "--reverse", f"{source}..HEAD")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def upstream_commit_list(repo_root: Path, base: str, source: str) -> list[str]:
    """Return upstream first-parent commits between *base* and *source*, oldest first."""
    result = run_git(
        repo_root,
        "rev-list",
        "--reverse",
        "--first-parent",
        f"{base}..{source}",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def commit_subject(repo_root: Path, commit: str) -> str:
    """Return a short subject line for *commit*."""
    result = run_git(repo_root, "show", "-s", "--format=%h %s", commit)
    return result.stdout.strip()


def has_unmerged_paths(repo_root: Path) -> bool:
    """Return True when git reports unmerged paths."""
    result = run_git(
        repo_root,
        "diff",
        "--name-only",
        "--diff-filter=U",
        check=False,
    )
    return bool(result.stdout.strip())


def has_rebase_head(repo_root: Path) -> bool:
    """Return True when a rebase is in progress."""
    result = run_git(
        repo_root,
        "rev-parse",
        "-q",
        "--verify",
        "REBASE_HEAD",
        check=False,
    )
    return result.returncode == 0


def rebase_one_step(
    repo_root: Path,
    new_base: str,
    old_base: str,
    rebase_merges: bool,
) -> subprocess.CompletedProcess[str]:
    """Replay local commits from *old_base* onto *new_base*."""
    args = ["rebase"]
    if rebase_merges:
        args.append("--rebase-merges")
    args.extend(["--onto", new_base, old_base])
    return run_git(repo_root, *args, check=False, timeout=LONG_TIMEOUT_SEC)


def rebase_to_source(repo_root: Path, source: str, rebase_merges: bool) -> subprocess.CompletedProcess[str]:
    """Fast-forward or plain rebase when no stepwise replay is needed."""
    args = ["rebase"]
    if rebase_merges:
        args.append("--rebase-merges")
    args.append(source)
    return run_git(repo_root, *args, check=False, timeout=LONG_TIMEOUT_SEC)


def print_commit_plan(title: str, commits: list[str], repo_root: Path) -> None:
    """Print a numbered list of commit subjects."""
    print(title)
    for index, commit in enumerate(commits, start=1):
        print(f"{index:>4}. {commit_subject(repo_root, commit)}")


def main() -> int:
    args = parse_args()

    try:
        repo_root = detect_repo_root()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        branch = current_branch(repo_root)
        sync_local_source_branch(repo_root, args.source, branch)
        if args.fetch and not local_branch_exists(repo_root, args.source):
            maybe_fetch(repo_root, args.source)

        if not args.allow_dirty and working_tree_dirty(repo_root):
            print(
                "Working tree is not clean. Commit or stash your changes first, "
                "or rerun with --allow-dirty.",
                file=sys.stderr,
            )
            return 1

        if has_rebase_head(repo_root):
            if has_unmerged_paths(repo_root):
                print(
                    "A rebase is in progress with unmerged paths. Resolve conflicts and "
                    "run 'git rebase --continue' first.",
                    file=sys.stderr,
                )
                return 1
            # Stale REBASE_HEAD with no unmerged paths — clean it up.
            run_git(repo_root, "update-ref", "-d", "REBASE_HEAD", check=False)

        base = merge_base(repo_root, args.source)
        head = current_head(repo_root)
        source_head = resolve_ref(repo_root, args.source)
        local_commits = local_commit_list(repo_root, args.source)
        upstream_commits = upstream_commit_list(repo_root, base, args.source)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Repository: {repo_root}")
    print(f"Current branch: {branch}")
    print(f"Source ref: {args.source}")
    print(f"Merge base: {base}")
    print(f"Upstream first-parent commits to advance through: {len(upstream_commits)}")
    print(f"Local commits to replay: {len(local_commits)}")

    fast_forward_only = not local_commits and head != source_head and is_ancestor(repo_root, head, source_head)
    already_at_source = not local_commits and head == source_head

    if already_at_source:
        print("Current branch is already at the source ref.")
        return 0

    if args.dry_run:
        print("")
        print("Dry run only.")
        if upstream_commits:
            print_commit_plan(
                "The branch would advance through these upstream first-parent commits:",
                upstream_commits,
                repo_root,
            )
        if local_commits:
            print_commit_plan(
                "The following local commits would be replayed at each step:",
                local_commits,
                repo_root,
            )
        if fast_forward_only:
            print("Current branch would fast-forward to the source ref.")
        elif not upstream_commits:
            print("No upstream commits need to be applied.")
        return 0

    if fast_forward_only:
        result = rebase_to_source(repo_root, args.source, args.rebase_merges)
        if result.returncode != 0:
            print("", file=sys.stderr)
            print("git rebase failed.", file=sys.stderr)
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
            elif result.stdout.strip():
                print(result.stdout.strip(), file=sys.stderr)
            return 1
        print("")
        print("Rebase finished without conflicts.")
        print("Current branch fast-forwarded to the source ref.")
        return 0

    advanced = 0
    current_base = base

    for index, upstream_commit in enumerate(upstream_commits, start=1):
        subject = commit_subject(repo_root, upstream_commit)
        print(f"[{index}/{len(upstream_commits)}] Rebasing onto upstream commit: {subject}")
        result = rebase_one_step(
            repo_root,
            new_base=upstream_commit,
            old_base=current_base,
            rebase_merges=args.rebase_merges,
        )
        if result.returncode != 0:
            if has_unmerged_paths(repo_root) or has_rebase_head(repo_root):
                print("", file=sys.stderr)
                print(
                    f"Conflict encountered while advancing onto upstream commit: {subject}",
                    file=sys.stderr,
                )
                print("Resolve the conflict and continue with:", file=sys.stderr)
                print("  git rebase --continue", file=sys.stderr)
                print("After that step completes, rerun this script to keep advancing.", file=sys.stderr)
                print("Or abort the in-progress rebase with:", file=sys.stderr)
                print("  git rebase --abort", file=sys.stderr)
                return 1

            print("", file=sys.stderr)
            print("git rebase failed.", file=sys.stderr)
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
            elif result.stdout.strip():
                print(result.stdout.strip(), file=sys.stderr)
            return 1

        current_base = upstream_commit
        advanced += 1

        # Run sanity checks after each successful step
        _run_post_rebase_sanity_checks(repo_root)

        # Clean up any stale REBASE_HEAD left by a successful rebase.
        # Some git versions leave this ref behind after `git rebase`
        # completes without conflicts, which would cause has_rebase_head()
        # to reject the next script invocation.
        run_git(repo_root, "update-ref", "-d", "REBASE_HEAD", check=False)

    print("")
    print("Rebase finished without conflicts.")
    print(f"Advanced through upstream commits: {advanced}")
    if local_commits:
        print(f"Replayed local commits at each step: {len(local_commits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
