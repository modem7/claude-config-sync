"""Entry point for `python -m claude_sync`."""

from __future__ import annotations

import os
import signal
import sys

MIN_PYTHON = (3, 12)


def _exit_on(code: int):
    def handler(_signum, _frame):
        raise SystemExit(code)

    return handler


def install_signal_handlers() -> None:
    """As bash's traps (exit 143 / 129): end the run through the entry
    point's handlers, so the lock and temporary files are let go, no traceback."""
    for name, code in (("SIGTERM", 143), ("SIGHUP", 129)):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _exit_on(code))


def safe_streams() -> None:
    """stdout and stderr write a character their encoding lacks (a
    redirected stream in a legacy Windows code page) as a backslash escape
    instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # closed, or not reconfigurable
                pass


def main(argv: list[str] | None = None) -> int:
    """Runs cli.main with the process environment and returns its exit code."""
    if sys.version_info < MIN_PYTHON:
        print("claude-sync needs Python 3.12 or newer.", file=sys.stderr)
        return 1
    # Imported only now: the modules use 3.12 APIs.
    from claude_sync import cli

    safe_streams()
    install_signal_handlers()
    return cli.main(sys.argv[1:] if argv is None else argv, os.environ)


if __name__ == "__main__":
    sys.exit(main())
