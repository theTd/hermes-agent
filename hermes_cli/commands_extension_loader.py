"""Branch-local CLI command loader."""

from __future__ import annotations

from typing import Any


def register_active_branch_cli_commands(subparsers: Any) -> None:
    """Register the active branch CLI command set."""
    from hermes_cli.napcat_commands import register_napcat_cli_commands

    register_napcat_cli_commands(subparsers)
