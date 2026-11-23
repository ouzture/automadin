from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from automadin.services.database import DatabaseService


CURRENT_SETUP_VERSION = 1
SETUP_ACKNOWLEDGED_VERSION_SETTING = "setup_center.acknowledged_version"
SETUP_SELECTED_TRACK_SETTING = "setup_center.selected_track"
DEFAULT_ACKNOWLEDGED_VERSION = 0


class SetupTrack(str, Enum):
    BROWSER = "browser"
    ANDROID = "android"
    WINDOWS = "windows"


@dataclass(frozen=True)
class SetupCenterPreferences:
    acknowledged_version: int = DEFAULT_ACKNOWLEDGED_VERSION
    selected_track: SetupTrack | None = None


class SetupCenterService:
    """Load and persist the versioned state of the Setup Center."""

    def __init__(self, database: DatabaseService) -> None:
        self._database = database

    def load_preferences(self) -> SetupCenterPreferences:
        return SetupCenterPreferences(
            acknowledged_version=self._load_acknowledged_version(),
            selected_track=self._load_selected_track(),
        )

    def should_auto_open(self) -> bool:
        return self._load_acknowledged_version() < CURRENT_SETUP_VERSION

    def acknowledge_current(self) -> None:
        acknowledged_version = max(
            self._load_acknowledged_version(),
            CURRENT_SETUP_VERSION,
        )
        self._database.set_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING,
            str(acknowledged_version),
        )

    def reset_acknowledgement(self) -> None:
        self._database.delete_app_setting(SETUP_ACKNOWLEDGED_VERSION_SETTING)

    def set_selected_track(self, track: SetupTrack) -> None:
        if not isinstance(track, SetupTrack):
            raise ValueError(f"Unsupported setup track: {track!r}")
        self._database.set_app_setting(SETUP_SELECTED_TRACK_SETTING, track.value)

    def clear_selected_track(self) -> None:
        self._database.delete_app_setting(SETUP_SELECTED_TRACK_SETTING)

    def _load_acknowledged_version(self) -> int:
        raw_value = self._database.get_app_setting(
            SETUP_ACKNOWLEDGED_VERSION_SETTING
        )
        try:
            acknowledged_version = int(raw_value) if raw_value is not None else -1
        except (TypeError, ValueError):
            return DEFAULT_ACKNOWLEDGED_VERSION
        if acknowledged_version < 0:
            return DEFAULT_ACKNOWLEDGED_VERSION
        return acknowledged_version

    def _load_selected_track(self) -> SetupTrack | None:
        raw_value = self._database.get_app_setting(SETUP_SELECTED_TRACK_SETTING)
        try:
            return SetupTrack(raw_value) if raw_value is not None else None
        except ValueError:
            return None
