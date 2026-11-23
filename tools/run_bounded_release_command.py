"""Run one frozen release gate in a bounded, kill-on-close Windows Job Object."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from tools.smoke_windows_release import ManagedProcess, SmokeFailure, managed_process


ProcessFactory = Callable[
    [list[str], dict[str, str]],
    AbstractContextManager[ManagedProcess],
]


def run_bounded_release_command(
    command: Sequence[str],
    *,
    timeout: float,
    environment: dict[str, str] | None = None,
    process_factory: ProcessFactory = managed_process,
) -> None:
    """Require a clean exit before timeout; context cleanup terminates the tree."""
    if timeout <= 0:
        raise ValueError("Timeout must be greater than zero.")
    if not command:
        raise ValueError("A release command is required.")
    executable = Path(command[0]).expanduser().resolve(strict=True)
    if not executable.is_file():
        raise FileNotFoundError(f"Release executable is not a file: {executable}")
    child_environment = dict(os.environ if environment is None else environment)
    with process_factory(
        [str(executable), *command[1:]],
        child_environment,
    ) as process:
        process.require_clean_exit(timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("executable")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = list(args.arguments)
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    try:
        run_bounded_release_command(
            [args.executable, *arguments],
            timeout=args.timeout,
        )
    except (OSError, SmokeFailure, ValueError) as exc:
        print(f"BOUNDED RELEASE COMMAND FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
