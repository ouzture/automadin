from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


mode, target = sys.argv[1:3]
result_path = Path(os.environ["AUTOMADIN_BROWSER_HEALTH_RESULT"])
containment_ready = Path(os.environ["AUTOMADIN_BROWSER_HEALTH_CONTAINED"])
deadline = time.monotonic() + 2
while not containment_ready.is_file():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.01)


def write(payload: bytes) -> None:
    result_path.write_bytes(payload)


def spawn_delayed_sentinel() -> None:
    sentinel = Path(sys.argv[3])
    script = (
        "import pathlib,sys,time;"
        "time.sleep(0.4);"
        "pathlib.Path(sys.argv[1]).write_text('leaked', encoding='utf-8');"
        "time.sleep(30)"
    )
    subprocess.Popen(
        [sys.executable, "-c", script, str(sentinel)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if mode in {"success", "environment", "success_child"}:
    assert "AUTOMADIN_PROFILES_DIR" not in os.environ
    assert Path(os.environ["TMP"]).resolve() == Path.cwd().resolve()
    if mode == "environment":
        assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).resolve() == Path(
            sys.argv[3]
        ).resolve()
    elif mode == "success_child":
        spawn_delayed_sentinel()
    write(
        (
            json.dumps(
            {
                "target": target,
                "state": "healthy",
                "stage": "complete",
                "message": "The isolated headless launch check completed.",
            },
            separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
elif mode == "malformed":
    write(b"not-json\n")
elif mode == "duplicate":
    write(b"{}\n{}\n")
elif mode == "malformed_child":
    spawn_delayed_sentinel()
    write(b"not-json\n")
elif mode == "nonzero_child":
    spawn_delayed_sentinel()
    raise SystemExit(4)
elif mode == "child_hang":
    spawn_delayed_sentinel()
    while True:
        time.sleep(0.05)
elif mode == "hang":
    while True:
        time.sleep(0.05)
else:
    raise SystemExit(2)
