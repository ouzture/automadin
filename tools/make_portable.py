"""Create a ZIP64 portable release without duplicating the unpacked payload."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from verify_release import verify_release


def make_portable(source: Path, destination: Path) -> None:
    errors = verify_release(source)
    if errors:
        raise RuntimeError("Release verification failed:\n" + "\n".join(errors))

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    root_name = "Automadin-Portable"
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(source).as_posix()
                    archive.write(path, f"{root_name}/{relative}")
            archive.writestr(f"{root_name}/portable.flag", "")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    make_portable(args.source.resolve(), args.destination.resolve())
    print(f"Portable archive ready: {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
