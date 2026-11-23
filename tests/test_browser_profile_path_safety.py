from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from automadin.engine.browser_engine import (
    BrowserDeviceEngine,
    BrowserProfileLaunchError,
)
from automadin.models.device import BrowserDevice, BrowserDeviceConfig, BrowserEngine
from automadin.services.browser_profile_manifest import (
    BrowserProfileManifest,
    InvalidBrowserProfileManifestError,
    write_managed_profile_manifest,
    _write_profile_manifest as write_profile_manifest,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_paths import (
    UnsafeBrowserProfileNameError,
    ensure_profile_storage_directory,
    resolve_managed_profile_path,
)
from automadin.services.database import DatabaseService
from automadin.ui.launcher.launcher_view import _profile_reference_error


def _maintenance_lease(profile_id: str):
    return BrowserProfileLeaseRegistry().acquire_maintenance(
        profile_id,
        ProfileUse("test", "Browser profile path safety test", str(uuid.uuid4())),
    )


def _browser_device(profile_name: str) -> BrowserDevice:
    return BrowserDevice(
        config=BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            profile_name=profile_name,
        )
    )


def _stable_browser_device(
    profile_id: str,
    *,
    engine: BrowserEngine = BrowserEngine.CHROMIUM,
    name: str = "Managed profile",
) -> BrowserDevice:
    return BrowserDevice(
        config=BrowserDeviceConfig(
            engine=engine,
            profile_name=name,
            profile_id=profile_id,
        )
    )


def test_profile_resolver_accepts_leaf_display_names() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "profiles"
        assert resolve_managed_profile_path("Jane - Work Account", root) == (
            Path(os.path.abspath(root)) / "Jane - Work Account"
        )


def test_profile_resolver_rejects_cross_platform_escape_syntax() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "profiles"
        unsafe_names = (
            "../outside",
            "folder/nested",
            r"..\outside",
            r"folder\nested",
            "/absolute/profile",
            r"C:\absolute\profile",
            r"C:drive-relative",
            r"\\server\share\profile",
            ".",
            "..",
        )
        for profile_name in unsafe_names:
            with pytest.raises(UnsafeBrowserProfileNameError, match="managed profiles"):
                resolve_managed_profile_path(profile_name, root)


def test_database_rejects_new_unsafe_profile_names() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        profiles = root / "profiles"
        with patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}):
            db = DatabaseService(str(root / "automadin.db"))
            try:
                with pytest.raises(UnsafeBrowserProfileNameError):
                    db.save_profile("../outside", browser="Chromium", engine="chromium")
                assert db.list_profiles() == []
            finally:
                db.close()


def test_database_readiness_ignores_unsafe_imported_profile_records() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        profiles = root / "profiles"
        outside = root / "outside"
        (outside / "Default").mkdir(parents=True)
        with patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}):
            db = DatabaseService(str(root / "automadin.db"))
            try:
                # Model a legacy/imported database that predates write validation.
                db._conn.execute(
                    """INSERT INTO profiles (
                           name, created_at, browser, source_profile,
                           account_name, account_email, engine
                       ) VALUES (?, ?, ?, '', '', '', ?)""",
                    ("../outside", "2026-01-01T00:00:00+00:00", "Chromium", "chromium"),
                )
                db._conn.commit()

                assert db.list_profiles() == ["../outside"]
                assert db.profile_engine_specs() == []
            finally:
                db.close()


def test_launcher_preflight_reports_unsafe_saved_profile_without_readiness_lookup() -> None:
    class _UnsafeProfileDb:
        @staticmethod
        def list_profiles() -> list[str]:
            return ["../outside"]

        @staticmethod
        def profile_engine_specs() -> list[tuple[str, str]]:
            raise AssertionError("unsafe references must fail before filesystem readiness")

    with tempfile.TemporaryDirectory() as temp_dir:
        profiles = Path(temp_dir) / "profiles"
        with patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}):
            error = _profile_reference_error(
                _UnsafeProfileDb(),
                _browser_device("../outside"),
            )

    assert error is not None
    assert "unsafe and was blocked" in error
    assert "choose a valid managed profile" in error


def test_browser_launch_rejects_unresolved_legacy_profile_before_playwright() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        profiles = root / "profiles"
        with (
            patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}),
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
        ):
            with pytest.raises(BrowserProfileLaunchError, match="unresolved legacy"):
                BrowserDeviceEngine().connect(_browser_device("Legacy profile"))

        starter.assert_not_called()
        assert not profiles.exists()


def test_browser_launch_does_not_create_missing_uuid_storage() -> None:
    profile_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as temp_dir:
        profiles = Path(temp_dir) / "profiles"
        with (
            patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}),
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
        ):
            with pytest.raises(
                InvalidBrowserProfileManifestError,
                match="does not exist",
            ):
                BrowserDeviceEngine().connect(_stable_browser_device(profile_id))

        starter.assert_not_called()
        assert not profiles.exists()


def test_browser_launch_rejects_manifest_identity_mismatch_before_playwright() -> None:
    directory_id = str(uuid.uuid4())
    manifest_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as temp_dir:
        profiles = Path(temp_dir) / "profiles"
        with _maintenance_lease(directory_id) as lease:
            directory = ensure_profile_storage_directory(
                directory_id,
                profiles,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        write_profile_manifest(
            directory,
            BrowserProfileManifest.create("chromium", profile_id=manifest_id),
        )
        (directory / "Default").mkdir()
        with (
            patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}),
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
        ):
            with pytest.raises(
                InvalidBrowserProfileManifestError,
                match="identity does not match",
            ):
                BrowserDeviceEngine().connect(_stable_browser_device(directory_id))

        starter.assert_not_called()


def test_browser_launch_rejects_manifest_engine_and_layout_before_playwright() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        profiles = Path(temp_dir) / "profiles"
        engine_mismatch_id = str(uuid.uuid4())
        with _maintenance_lease(engine_mismatch_id) as lease:
            write_managed_profile_manifest(
                BrowserProfileManifest.create(
                    "firefox",
                    profile_id=engine_mismatch_id,
                ),
                profiles,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        missing_layout_id = str(uuid.uuid4())
        with _maintenance_lease(missing_layout_id) as lease:
            write_managed_profile_manifest(
                BrowserProfileManifest.create(
                    "chromium",
                    profile_id=missing_layout_id,
                ),
                profiles,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )

        with (
            patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}),
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
        ):
            with pytest.raises(BrowserProfileLaunchError, match="requires engine"):
                BrowserDeviceEngine().connect(
                    _stable_browser_device(engine_mismatch_id)
                )
            with pytest.raises(BrowserProfileLaunchError, match="compatible managed"):
                BrowserDeviceEngine().connect(
                    _stable_browser_device(missing_layout_id)
                )

        starter.assert_not_called()


def test_browser_launch_rejects_redirected_default_directory_before_playwright() -> None:
    profile_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        profiles = root / "profiles"
        with _maintenance_lease(profile_id) as lease:
            directory = ensure_profile_storage_directory(
                profile_id,
                profiles,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        write_profile_manifest(
            directory,
            BrowserProfileManifest.create("chromium", profile_id=profile_id),
        )
        outside = root / "outside-default"
        outside.mkdir()
        try:
            (directory / "Default").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as exc:
            pytest.skip(f"Directory links are unavailable: {exc}")

        with (
            patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}),
            patch("automadin.engine.browser_engine.sync_playwright") as starter,
        ):
            with pytest.raises(BrowserProfileLaunchError, match="compatible managed"):
                BrowserDeviceEngine().connect(_stable_browser_device(profile_id))

        starter.assert_not_called()


def test_internal_browser_launch_boundary_revalidates_manifest_and_uses_uuid_path() -> None:
    profile_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as temp_dir:
        profiles = Path(temp_dir) / "profiles"
        with _maintenance_lease(profile_id) as lease:
            manifest_path = write_managed_profile_manifest(
                BrowserProfileManifest.create("chromium", profile_id=profile_id),
                profiles,
                lease_registry=lease._registry,
                maintenance_lease=lease,
            )
        (manifest_path.parent / "Default").mkdir()
        playwright = Mock()
        context = playwright.chromium.launch_persistent_context.return_value
        context.pages = []
        with patch.dict(os.environ, {"AUTOMADIN_PROFILES_DIR": str(profiles)}):
            BrowserDeviceEngine()._connect_started(
                playwright,
                _stable_browser_device(profile_id).config,
            )

        playwright.chromium.launch_persistent_context.assert_called_once()
        assert (
            playwright.chromium.launch_persistent_context.call_args.args[0]
            == str(profiles / profile_id)
        )
