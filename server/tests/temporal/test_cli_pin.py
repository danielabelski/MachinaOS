"""The pinned Temporal CLI and the database its dev server may open.

RFC-0004 D1/Q3: new installs use the checksum pin; an upgrade preserves
the existing server/database pair until an explicit migration.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.temporal import _install, _runtime


def _db_files(db: Path) -> list[Path]:
    return [db, Path(f"{db}-wal"), Path(f"{db}-shm")]


def _write_db(db: Path) -> None:
    for path in _db_files(db):
        path.write_bytes(b"temporal state")


def _write_marker(db: Path, server_version: str) -> None:
    _install.marker_path(db).write_text(json.dumps({"server_version": server_version}), encoding="utf-8")


def _same_minor_other_patch() -> str:
    major, minor, _patch = _install.cli_pin()["server_version"].split(".")
    return f"{major}.{minor}.99"


def _other_minor() -> str:
    major, minor, _patch = _install.cli_pin()["server_version"].split(".")
    return f"{major}.{int(minor) - 1}.0"


def test_pin_names_one_exact_release_with_a_hash_for_every_platform():
    pin = _install.cli_pin()

    assert "latest" not in _install.PIN_PATH.read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["server_version"])
    assert "{version}" in pin["base"]
    assert set(pin["assets"]) == {f"{os_}_{arch}" for os_, arch in _install._CLI_PLATFORM_MAP.values()}
    for key, asset in pin["assets"].items():
        extension = "zip" if key.startswith("windows_") else "tar.gz"
        assert asset["file"] == f"temporal_cli_{pin['version']}_{key}.{extension}"
        assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])


def test_fetch_verifies_the_pinned_archive_and_preserves_older_clis(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(_install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_install.platform, "machine", lambda: "x86_64")
    (tmp_path / "temporal_cli_latest_linux_amd64.tar.gz").write_bytes(b"unpinned archive")
    (tmp_path / "temporal_cli_latest_linux_amd64.tar.gz.untar").mkdir()
    (tmp_path / "temporal_cli_latest_linux_amd64.tar.gz.untar" / "temporal").write_bytes(b"unpinned")
    (tmp_path / "1.0.0").mkdir()
    calls = []

    def fake_retrieve(**kwargs):
        calls.append(kwargs)
        extracted = Path(kwargs["path"]) / f"{kwargs['fname']}.untar"
        extracted.mkdir(parents=True)
        binary = extracted / "temporal"
        binary.write_bytes(b"pinned")
        return [str(extracted / "LICENSE"), str(binary)]

    monkeypatch.setattr(_install.pooch, "retrieve", fake_retrieve)

    binary = _install._fetch_cli_sync()

    pin = _install.cli_pin()
    asset = pin["assets"]["linux_amd64"]
    assert len(calls) == 1
    assert calls[0]["url"].endswith(f"/v{pin['version']}/{asset['file']}")
    assert "latest" not in calls[0]["url"]
    assert calls[0]["known_hash"] == f"sha256:{asset['sha256']}"
    assert Path(calls[0]["path"]) == tmp_path / pin["version"]
    assert calls[0]["fname"] == asset["file"]
    assert binary == tmp_path / pin["version"] / f"{asset['file']}.untar" / "temporal"
    assert (tmp_path / "1.0.0").is_dir()
    assert (tmp_path / "temporal_cli_latest_linux_amd64.tar.gz").read_bytes() == b"unpinned archive"
    assert (tmp_path / pin["version"]).is_dir()


def test_only_the_pinned_cli_counts_as_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path)
    unpinned = tmp_path / "temporal_cli_latest_windows_amd64.zip.unzip"
    unpinned.mkdir()
    (unpinned / _install._binary_name()).write_bytes(b"unpinned")

    assert _install.binary_installed() is False

    pinned = tmp_path / _install.cli_pin()["version"] / "archive.unzip"
    pinned.mkdir(parents=True)
    (pinned / _install._binary_name()).write_bytes(b"pinned")

    assert _install.binary_installed() is True


def test_a_new_install_records_the_pinned_server_beside_its_database(tmp_path):
    db = tmp_path / "temporal.db"

    _install.prepare_database(db)

    pin = _install.cli_pin()
    assert not db.exists()
    marker = json.loads(_install.marker_path(db).read_text(encoding="utf-8"))
    assert marker["cli_version"] == pin["version"]
    assert marker["server_version"] == pin["server_version"]
    assert marker["created_at"]


def test_unmarked_database_is_never_deleted_or_guessed(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    with pytest.raises(RuntimeError, match="original binary"):
        _install.prepare_database(db)
    assert all(path.read_bytes() == b"temporal state" for path in _db_files(db))
    assert not _install.marker_path(db).exists()


def test_another_server_minor_requires_explicit_migration(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _write_marker(db, _other_minor())
    before = _install.marker_path(db).read_bytes()
    with pytest.raises(RuntimeError, match="explicit migration"):
        _install.prepare_database(db)
    assert all(path.read_bytes() == b"temporal state" for path in _db_files(db))
    assert _install.marker_path(db).read_bytes() == before


def test_a_patch_release_of_the_same_minor_keeps_the_database(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _write_marker(db, _same_minor_other_patch())

    _install.prepare_database(db)

    assert all(path.read_bytes() == b"temporal state" for path in _db_files(db))


def test_unreadable_marker_refuses_startup_and_preserves_database(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _install.marker_path(db).write_text("not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Cannot verify"):
        _install.prepare_database(db)
    assert all(path.exists() for path in _db_files(db))


def test_the_dev_server_prepares_its_database_before_it_spawns(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        temporal_graceful_shutdown_seconds=5,
        temporal_sqlite_path="data/temporal.db",
        _resolve_under_data=lambda path: str(tmp_path / path),
    )
    runtime = _runtime.TemporalServerRuntime(settings=settings)
    prepared = []

    async def fake_binaries(_settings):
        return {"temporal": tmp_path / "temporal"}

    monkeypatch.setattr(_install, "ensure_temporal_binaries", fake_binaries)
    monkeypatch.setattr(_install, "prepare_database", lambda db, binary: prepared.append((db, binary)))

    asyncio.run(runtime._pre_spawn())

    assert prepared == [(tmp_path / "data" / "temporal.db", tmp_path / "temporal")]
    assert (tmp_path / "data").is_dir()


def test_runtime_adopts_the_existing_legacy_cli_for_an_unmarked_database(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path / "packages")
    monkeypatch.setattr(_install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_install.platform, "machine", lambda: "AMD64")
    binary = tmp_path / "packages" / "temporal_cli_latest_windows_amd64.zip.unzip" / "temporal.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"original")
    monkeypatch.setattr(_install, "_binary_versions", lambda path: {"cli_version": "1.8.0", "server_version": "1.31.2"})
    db = tmp_path / "temporal.db"
    _write_db(db)
    assert _install._existing_database_cli(db) == binary
    _install.prepare_database(db, binary)
    marker = json.loads(_install.marker_path(db).read_text(encoding="utf-8"))
    assert marker["server_version"] == "1.31.2"
    assert marker["cli_version"] == "1.8.0"
    assert _install._existing_database_cli(db) == binary.resolve()
    assert all(path.read_bytes() == b"temporal state" for path in _db_files(db))


def test_missing_original_binary_refuses_startup_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path / "packages")
    db = tmp_path / "temporal.db"
    _write_db(db)
    with pytest.raises(RuntimeError, match="original Temporal binary"):
        _install._existing_database_cli(db)
    assert all(path.exists() for path in _db_files(db))


def test_original_marker_retains_older_versioned_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path / "packages")
    monkeypatch.setattr(_install, "_binary_name", lambda: "temporal")
    monkeypatch.setattr(_install, "_binary_versions", lambda path: {"cli_version": "1.8.0", "server_version": "1.31.2"})
    binary = tmp_path / "packages" / "1.8.0" / "archive.untar" / "temporal"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"original")
    db = tmp_path / "temporal.db"
    _write_db(db)
    _install.marker_path(db).write_text(json.dumps({"cli_version": "1.8.0", "server_version": "1.31.2"}), encoding="utf-8")
    assert _install._existing_database_cli(db) == binary


def test_tampered_server_identity_cannot_open_a_database(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path / "packages")
    monkeypatch.setattr(_install, "_binary_name", lambda: "temporal")
    monkeypatch.setattr(_install, "_binary_versions", lambda path: {"cli_version": "1.8.0", "server_version": "1.32.0"})
    binary = tmp_path / "packages" / "1.8.0" / "temporal"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"wrong")
    db = tmp_path / "temporal.db"
    _write_db(db)
    _install.marker_path(db).write_text(json.dumps({"cli_version": "1.8.0", "server_version": "1.31.2"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="mismatch"):
        _install._existing_database_cli(db)
    assert all(path.exists() for path in _db_files(db))


def test_marker_version_cannot_escape_the_package_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(_install, "_cache_dir", lambda: tmp_path / "packages")
    db = tmp_path / "temporal.db"
    _write_db(db)
    _install.marker_path(db).write_text(json.dumps({"cli_version": "../other", "server_version": "1.31.2"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot identify the original Temporal CLI"):
        _install._existing_database_cli(db)
    assert all(path.exists() for path in _db_files(db))
