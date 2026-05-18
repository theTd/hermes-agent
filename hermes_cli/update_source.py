"""Helpers for syncing updates from the standalone release repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_UPDATE_OWNER = os.environ.get("HERMES_UPDATE_OWNER", "theTd")
_UPDATE_REPO = os.environ.get("HERMES_UPDATE_REPO", "hermes-agent")
STANDALONE_UPDATE_BRANCH = os.environ.get("HERMES_UPDATE_BRANCH", "napcat")
STANDALONE_REPO_URL = f"https://github.com/{_UPDATE_OWNER}/{_UPDATE_REPO}.git"
STANDALONE_REPO_URLS = {
    STANDALONE_REPO_URL,
    f"git@github.com:{_UPDATE_OWNER}/{_UPDATE_REPO}.git",
    f"https://github.com/{_UPDATE_OWNER}/{_UPDATE_REPO}",
    f"git@github.com:{_UPDATE_OWNER}/{_UPDATE_REPO}",
}
STANDALONE_REMOTE_REF_PREFIX = "refs/remotes/hermes-standalone"


def normalize_repo_url(url: str | None) -> str | None:
    """Normalize a git remote URL for equality checks."""
    if not url:
        return None
    normalized = url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def is_standalone_repo_url(url: str | None) -> bool:
    """Return True if *url* points at the standalone release repo."""
    normalized = normalize_repo_url(url)
    if not normalized:
        return False
    for repo_url in STANDALONE_REPO_URLS:
        if normalized == normalize_repo_url(repo_url):
            return True
    return False


def standalone_update_ref(branch: str = STANDALONE_UPDATE_BRANCH) -> str:
    """Return the local tracking ref used for standalone update checks."""
    return f"{STANDALONE_REMOTE_REF_PREFIX}/{branch}"


def standalone_update_label(branch: str = STANDALONE_UPDATE_BRANCH) -> str:
    """Return a short user-facing label for the standalone update source."""
    return f"theTd/hermes-agent:{branch}"


def standalone_zip_url(branch: str = STANDALONE_UPDATE_BRANCH) -> str:
    """Return the ZIP archive URL for the standalone update branch."""
    return f"https://github.com/theTd/hermes-agent/archive/refs/heads/{branch}.zip"


def standalone_install_command(branch: str = STANDALONE_UPDATE_BRANCH) -> str:
    """Return the reinstall command for standalone installs."""
    return (
        "curl -fsSL "
        f"https://raw.githubusercontent.com/theTd/hermes-agent/{branch}/scripts/install.sh"
        " | bash"
    )


def fetch_standalone_update_ref(
    git_cmd: list[str],
    cwd: Path,
    branch: str = STANDALONE_UPDATE_BRANCH,
    *,
    quiet: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Fetch the standalone branch into a local tracking ref."""
    command = list(git_cmd)
    command.append("fetch")
    if quiet:
        command.append("--quiet")
    command.extend(
        [
            STANDALONE_REPO_URL,
            f"+refs/heads/{branch}:{standalone_update_ref(branch)}",
        ]
    )
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
