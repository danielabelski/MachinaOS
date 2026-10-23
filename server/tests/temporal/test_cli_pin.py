"""The pinned Temporal CLI and the database its dev server may open.

RFC-0004 D1 / AG1: the CLI comes from one exact release, checked against
a pinned SHA-256, never a "latest" URL. RFC-0005 E16: a database the
pinned server minor did not create is deleted with its WAL files, the
old CLI files are deleted, and the install starts on an empty Temporal
database.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

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


def test_fetch_verifies_the_pinned_archive_and_deletes_every_other_cli(tmp_path, monkeypatch):
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
    assert [entry.name for entry in tmp_path.iterdir()] == [pin["version"]]


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
    assert json.loads(_install.marker_path(db).read_text(encoding="utf-8")) == {
        "cli_version": pin["version"],
        "server_version": pin["server_version"],
    }


def test_a_database_from_before_the_pin_is_deleted(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)

    _install.prepare_database(db)

    assert not any(path.exists() for path in _db_files(db))
    assert json.loads(_install.marker_path(db).read_text(encoding="utf-8"))["server_version"] == _install.cli_pin()["server_version"]


def test_a_database_from_another_server_minor_is_deleted(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _write_marker(db, _other_minor())

    _install.prepare_database(db)

    assert not any(path.exists() for path in _db_files(db))
    assert json.loads(_install.marker_path(db).read_text(encoding="utf-8"))["server_version"] == _install.cli_pin()["server_version"]


def test_a_patch_release_of_the_same_minor_keeps_the_database(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _write_marker(db, _same_minor_other_patch())

    _install.prepare_database(db)

    assert all(path.read_bytes() == b"temporal state" for path in _db_files(db))


def test_an_unreadable_marker_cannot_keep_a_database(tmp_path):
    db = tmp_path / "temporal.db"
    _write_db(db)
    _install.marker_path(db).write_text("not json", encoding="utf-8")

    _install.prepare_database(db)

    assert not db.exists()


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
    monkeypatch.setattr(_install, "prepare_database", prepared.append)

    asyncio.run(runtime._pre_spawn())

    assert prepared == [tmp_path / "data" / "temporal.db"]
    assert (tmp_path / "data").is_dir()
