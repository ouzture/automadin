from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import run_bounded_release_command


class _Process:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def require_clean_exit(self, timeout: float) -> None:
        self.timeouts.append(timeout)


def test_bounded_runner_uses_exact_command_environment_and_timeout(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Automadin.exe"
    executable.write_bytes(b"MZ")
    process = _Process()
    calls: list[tuple[list[str], dict[str, str]]] = []

    @contextmanager
    def process_factory(command: list[str], environment: dict[str, str]):
        calls.append((command, environment))
        yield process

    run_bounded_release_command.run_bounded_release_command(
        [str(executable), "--release-test", "value"],
        timeout=12.5,
        environment={"ISOLATED": "1"},
        process_factory=process_factory,
    )

    assert calls == [
        (
            [str(executable.resolve()), "--release-test", "value"],
            {"ISOLATED": "1"},
        )
    ]
    assert process.timeouts == [12.5]


def test_bounded_runner_rejects_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_bounded_release_command.run_bounded_release_command(
            [str(tmp_path / "missing.exe")],
            timeout=1,
        )


def test_cli_preserves_frozen_arguments_after_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "Automadin.exe"
    executable.write_bytes(b"MZ")
    calls: list[tuple[list[str], float]] = []
    monkeypatch.setattr(
        run_bounded_release_command,
        "run_bounded_release_command",
        lambda command, *, timeout: calls.append((command, timeout)),
    )

    exit_code = run_bounded_release_command.main(
        [
            "--timeout",
            "60",
            str(executable),
            "--",
            "--release-diagnostics-self-test",
            "scratch",
            "--data-dir",
            "isolated",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            [
                str(executable),
                "--release-diagnostics-self-test",
                "scratch",
                "--data-dir",
                "isolated",
            ],
            60.0,
        )
    ]
