from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_READ_WRITE_PATHS = (
    "ReadWritePaths=/run/exocortex /var/lib/updater /etc/exocortex /opt/moonli"
)
LEGACY_INSTALL_ROOT = "/opt/exocortex"


def test_moonli_updater_unit_targets_the_moonli_install_root() -> None:
    unit = (ROOT / "deploy/updater.service").read_text(encoding="utf-8")

    assert EXPECTED_READ_WRITE_PATHS in unit
    assert LEGACY_INSTALL_ROOT not in unit


def test_release_builder_rejects_an_unpatched_updater_unit() -> None:
    builder = (ROOT / "scripts/build-release.sh").read_text(encoding="utf-8")

    assert 'updater_unit="$root/deploy/updater.service"' in builder
    assert f'expected_read_write_paths="{EXPECTED_READ_WRITE_PATHS}"' in builder
    assert "grep -Fqx \"$expected_read_write_paths\" \"$updater_unit\"" in builder
    assert f"grep -Fq '{LEGACY_INSTALL_ROOT}' \"$updater_unit\"" in builder
    assert 'install -m 0644 "$updater_unit"' in builder


def test_release_uses_the_updater_required_go_toolchain() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'go-version: "1.24.x"' in workflow


def test_installer_materializes_the_updater_control_token_alias() -> None:
    installer = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")

    assert "MOONLI_UPDATER_CONTROL_TOKEN=$updater_control" in installer
    assert "UPDATER_CONTROL_TOKEN=$updater_control" in installer
    assert "sync_updater_control_token()" in installer
    assert "install_service() {\n  sync_updater_control_token\n  validate" in installer
