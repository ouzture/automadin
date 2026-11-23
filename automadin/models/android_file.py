from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AndroidRootState:
    uid: int | None
    username: str
    raw: str

    @property
    def is_root(self) -> bool:
        return self.uid == 0

    @property
    def label(self) -> str:
        return self.username or (f"uid={self.uid}" if self.uid is not None else "unknown")


@dataclass(frozen=True)
class AndroidFileEntry:
    path: str
    name: str
    is_dir: bool
    size: int
    mode: str
    owner: str
    group: str
    modified: str

    @property
    def display_size(self) -> str:
        if self.is_dir:
            return "<DIR>"
        size = float(self.size)
        for suffix in ("B", "KB", "MB", "GB"):
            if size < 1024 or suffix == "GB":
                return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
            size /= 1024
        return f"{self.size} B"

    @property
    def is_regular_file(self) -> bool:
        return self.mode.startswith("-")


@dataclass(frozen=True)
class AndroidFilePreview:
    path: str
    kind: str
    title: str
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class PulledAndroidFile:
    device_path: str
    local_path: str
    size: int
