import tomllib
from pathlib import Path

import pytest

from tools.project_metadata import read_project_version
from tools.write_release_manifest import build_release_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_version_is_read_from_pyproject() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        configured_version = tomllib.load(pyproject_file)["project"]["version"]

    assert read_project_version(PROJECT_ROOT) == configured_version


def test_missing_project_version_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[project\]\.version"):
        read_project_version(tmp_path)


def test_release_files_do_not_duplicate_the_configured_version() -> None:
    version = read_project_version(PROJECT_ROOT)
    installer_script = (PROJECT_ROOT / "packaging" / "Automadin.iss").read_text(
        encoding="utf-8"
    )
    build_script = (PROJECT_ROOT / "tools" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert f'#define MyAppVersion "{version}"' not in installer_script
    assert f"Automadin-Portable-{version}.zip" not in build_script
    assert "#ifndef MyAppVersion" in installer_script
    assert "tools\\project_metadata.py" in build_script


def test_windows_build_selects_only_the_expected_deployment_output() -> None:
    build_script = (PROJECT_ROOT / "tools" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert '$ExpectedAppDir = Join-Path $DistRoot "Automadin.dist"' in build_script
    assert (
        '$GeneratedExecutablePath = Join-Path $ExpectedAppDir "main.exe"'
        in build_script
    )
    assert '$ExecutablePath = Join-Path $ExpectedAppDir "Automadin.exe"' in build_script
    assert "Get-ChildItem" not in build_script
    assert "Sort-Object LastWriteTime" not in build_script
    assert "$AppDir = $ExpectedAppDir" in build_script


def test_windows_build_prefers_real_inno_installation_over_path_shim() -> None:
    build_script = (PROJECT_ROOT / "tools" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    candidate_probe = (
        "Test-Path -LiteralPath $Candidate -PathType Leaf"
    )
    path_probe = 'Get-Command "ISCC.exe" -ErrorAction SilentlyContinue'
    license_probe = '$InnoLicense = Join-Path $InnoRoot "license.txt"'

    assert build_script.index(candidate_probe) < build_script.index(path_probe)
    assert build_script.index(path_probe) < build_script.index(license_probe)


def test_windows_build_cannot_reuse_a_stale_executable_after_compile() -> None:
    build_script = (PROJECT_ROOT / "tools" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    clean_output = (
        "Remove-Item -LiteralPath $ExpectedAppDir -Recurse -Force"
    )
    deploy = "& $Deploy -c $TemporarySpec -f"
    expected_output_check = (
        "Test-Path -LiteralPath $GeneratedExecutablePath -PathType Leaf"
    )

    assert clean_output in build_script
    assert build_script.index(clean_output) < build_script.index(deploy)
    assert build_script.index(deploy) < build_script.index(expected_output_check)
    assert (
        'throw "-SkipCompile requires the expected existing deployment executable: '
        '$ExecutablePath"'
        in build_script
    )
    version_check = (
        "Assert-ReleaseExecutableVersion $ExecutablePath $AppVersion"
    )
    assert version_check in build_script
    assert build_script.index(expected_output_check) < build_script.index(version_check)
    assert build_script.index(version_check) < build_script.index(
        "tools\\generate_release_compliance.py"
    )
    assert "FileVersion" in build_script
    assert "ProductVersion" in build_script


def test_frozen_build_declares_qt_network_for_instance_activation() -> None:
    deployment_spec = (PROJECT_ROOT / "pysidedeploy.spec").read_text(
        encoding="utf-8"
    )

    assert "modules = Core,Gui,Widgets,Network" in deployment_spec


def test_deployment_spec_is_machine_neutral_and_includes_runtime_dependencies() -> None:
    deployment_spec = (PROJECT_ROOT / "pysidedeploy.spec").read_text(encoding="utf-8")

    assert "C:\\Users\\" not in deployment_spec
    assert "python_path = __AUTOMADIN_PYTHON__" in deployment_spec
    assert "project_dir = __AUTOMADIN_PROJECT_DIR__" in deployment_spec
    assert "--file-version=__AUTOMADIN_VERSION__" in deployment_spec
    assert "--product-version=__AUTOMADIN_VERSION__" in deployment_spec
    # OpenCV loads optional Python subpackages dynamically. Its unused G-API
    # package conflicts with PySide's frozen-module inspection hook at startup.
    assert "--nofollow-import-to=cv2.gapi" in deployment_spec
    for module in (
        "automadin.browser_health_worker",
        "automadin.release_self_test",
        "automadin.diagnostics_self_test",
        "automadin.pushover_self_test",
        "automadin.support_report_worker",
        "automadin.services.support_report_process",
        "win32api",
        "win32event",
        "win32job",
        "win32con",
        "win32cred",
        "win32timezone",
        "win32process",
        "win32gui",
        "win32ui",
        "pywintypes",
    ):
        assert f"--include-module={module}" in deployment_spec
    assert "--include-distribution-metadata=playwright" in deployment_spec
    assert "--include-distribution-metadata=PySide6" in deployment_spec
    assert "--include-distribution-metadata=windows-cap" in deployment_spec
    assert "--include-package=windows_cap" in deployment_spec


def test_windows_build_uses_temporary_spec_and_runs_release_self_test() -> None:
    build_script = (PROJECT_ROOT / "tools" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert '[string]$Python,' in build_script
    assert "Resolve-PythonExecutable $Python" in build_script
    assert "Resolve-PySideDeployExecutable $Python" in build_script
    assert '(Join-Path $PythonDirectory "pyside6-deploy.exe")' in build_script
    assert (
        '(Join-Path $PythonDirectory "Scripts\\pyside6-deploy.exe")'
        in build_script
    )
    assert "& $Deploy -c $TemporarySpec -f" in build_script
    assert "Remove-Item -LiteralPath $TemporarySpec -Force" in build_script
    verify_position = build_script.index("tools\\verify_release.py")
    pushover_self_test_position = build_script.index(
        "--release-pushover-credential-self-test"
    )
    self_test_position = build_script.index(
        "--release-browser-health-self-test $ScratchParent"
    )
    diagnostics_self_test_position = build_script.index(
        "--release-diagnostics-self-test $ScratchParent"
    )
    portable_position = build_script.index("tools\\make_portable.py")
    assert (
        verify_position
        < pushover_self_test_position
        < self_test_position
        < diagnostics_self_test_position
        < portable_position
    )
    assert "Remove-Item -LiteralPath $ScratchParent -Recurse -Force" in build_script
    assert "-m tools.run_bounded_release_command --timeout 60" in build_script
    assert "--data-dir $FallbackDataDir" in build_script
    assert "Release Pushover credential self-test failed" in build_script
    assert "--release-self-test-nonce $DiagnosticsNonce" in build_script
    assert "diagnostics-$DiagnosticsNonce.ok" in build_script
    assert "Release diagnostics self-test marker was invalid." in build_script
    assert "Assert-ReleaseExecutableVersion $ExecutablePath $AppVersion" in build_script
    environment_check = build_script.index("tools\\verify_release_environment.py")
    deploy_position = build_script.index("& $Deploy -c $TemporarySpec -f")
    assert environment_check < deploy_position
    portable_cleanup = (
        "Remove-Item -LiteralPath $PortableArchive -Force"
    )
    installer_cleanup = "Remove-Item -LiteralPath $Installer -Force"
    assert portable_cleanup in build_script
    assert installer_cleanup in build_script
    assert build_script.index(portable_cleanup) < deploy_position
    assert build_script.index(installer_cleanup) < deploy_position


def test_release_manifest_uses_canonical_project_and_environment_versions() -> None:
    manifest = build_release_manifest(PROJECT_ROOT)

    assert manifest["schema_version"] == 1
    assert manifest["application"] == {
        "name": "Automadin",
        "version": read_project_version(PROJECT_ROOT),
    }
    assert manifest["build"]["python_version"]
    assert set(manifest["build"]["packages"]) == {
        "Nuitka",
        "PySide6",
        "playwright",
        "pywin32",
    }
