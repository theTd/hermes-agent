"""Generic branch CLI command loader."""

from __future__ import annotations

from typing import Any


def register_branch_cli_commands(subparsers: Any) -> None:
    """Register branch-local CLI commands without leaking implementation names."""
    try:
        from hermes_cli.commands_extension_loader import (
            register_active_branch_cli_commands,
        )

        register_active_branch_cli_commands(subparsers)
    except Exception:
        return
