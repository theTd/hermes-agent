"""Branch-local CLI entrypoints for NapCat-specific commands."""

from __future__ import annotations

from typing import Any


def cmd_napcat_monitor(args: Any) -> None:
    """Start the NapCat observability monitoring server."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("NapCat observability requires fastapi and uvicorn.")
        print("Install them with:  pip install hermes-agent[web]")
        raise SystemExit(1)

    from hermes_cli.napcat_observability_server import start_server

    start_server(
        host=args.host,
        port=args.port,
        allow_public=getattr(args, "insecure", False),
    )


def register_napcat_cli_commands(subparsers: Any) -> None:
    """Register NapCat-specific CLI subcommands."""
    napcat_monitor_parser = subparsers.add_parser(
        "napcat-monitor",
        help="Start the NapCat observability monitoring server",
        description="Launch the NapCat monitoring dashboard for real-time message tracing and diagnostics",
    )
    napcat_monitor_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port (default from config, fallback 9120)",
    )
    napcat_monitor_parser.add_argument(
        "--host",
        default=None,
        help="Host (default from config, fallback 127.0.0.1)",
    )
    napcat_monitor_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow binding to non-localhost addresses",
    )
    napcat_monitor_parser.set_defaults(func=cmd_napcat_monitor)
