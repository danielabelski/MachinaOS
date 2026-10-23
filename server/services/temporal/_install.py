"""The pinned Temporal CLI, and the database its dev server may open.

Downloads the official ``temporal`` CLI release named in
``server/config/temporal_cli.json`` (version, server version and each
asset's SHA-256) into ``<DATA_DIR>/packages/temporal/<version>/`` — the
same root that holds the Stripe and agent-browser binaries (see
:func:`core.paths.package_dir`). pooch verifies the archive against the
pinned hash before extracting it (zip on Windows, tar.gz elsewhere), so
a re-run is a cache hit and a tampered or truncated download fails.
Older binaries remain available beside the pin so existing databases keep
using the server that created them. Upgrading the application never migrates
or deletes Temporal state; a server minor change requires an explicit move.

A marker beside each database records the selected CLI/server and binary.
Unmarked pre-pin databases are adopted only with the existing legacy binary;
missing/ambiguous provenance refuses startup instead of guessing.

The downloaded ``temporal`` CLI powers ``temporal server start-dev``
(the SQLite dev server) and ad-hoc workflow / operator commands.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
import stat
from pathlib import Path
from typing import Any

import pooch

from core.config import Settings
from core.logging import get_logger

logger = get_logger(__name__)

# The one place the CLI version, its server version and the per-platform
# archive hashes live. Bumping it is a JSON edit plus checksums.txt.
PIN_PATH = Path(__file__).resolve().parents[2] / "config" / "temporal_cli.json"

_CLI_PLATFORM_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # platform.system, platform.machine -> (release os, release arch)
    ("Linux", "x86_64"): ("linux", "amd64"),
    ("Linux", "aarch64"): ("linux", "arm64"),
    ("Darwin", "x86_64"): ("darwin", "amd64"),
    ("Darwin", "arm64"): ("darwin", "arm64"),
    ("Windows", "AMD64"): ("windows", "amd64"),
    ("Windows", "ARM64"): ("windows", "arm64"),
}


def cli_pin() -> dict[str, Any]:
    """The pinned release: ``version``, ``server_version``, ``base``, ``assets``."""
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


def _cache_dir() -> Path:
    """Per-service install root under :func:`core.paths.package_dir`.

    Lands at ``<DATA_DIR>/packages/temporal/`` — same root that holds
    ``stripe`` and ``browser`` binaries. Each pinned version gets its own
    subfolder.
    """
    from core.paths import package_dir

    p = package_dir("temporal")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _binary_name() -> str:
    return "temporal.exe" if platform.system() == "Windows" else "temporal"


def binary_installed() -> bool:
    """True when the pinned CLI is already on disk, so starting needs no download."""
    root = _cache_dir() / cli_pin()["version"]
    return root.is_dir() and any(p.is_file() for p in root.rglob(_binary_name()))


_cached: dict[str, Path] | None = None
_lock = asyncio.Lock()


async def ensure_temporal_binaries(
    settings: Settings | None = None,
) -> dict[str, Path]:
    """Return ``{"temporal": Path}`` — the pinned ``temporal`` CLI binary.

    Idempotent — first call downloads, subsequent calls hit the pooch
    cache under ``<DATA_DIR>/packages/temporal/<version>/``.
    Async-locked so concurrent callers don't double-download.

    Runtime callers pass settings so an existing database selects its own
    installed server. Build callers omit it and materialize the release pin
    alongside older versions, without touching their databases.
    """
    global _cached
    async with _lock:
        if settings is not None:
            db = Path(settings._resolve_under_data(settings.temporal_sqlite_path))
            selected = await asyncio.to_thread(_existing_database_cli, db)
            if selected is not None:
                return {"temporal": selected}
        if _cached is not None and _cached["temporal"].is_file() and _cached["temporal"].resolve().is_relative_to(_cache_dir().resolve()):
            return _cached
        cli_path = await asyncio.to_thread(_fetch_cli_sync)
        _cached = {"temporal": cli_path}
        logger.info(
            "[Temporal install] binary ready: %s",
            {k: str(v) for k, v in _cached.items()},
        )
        return _cached


def _fetch_cli_sync() -> Path:
    """Download the pinned CLI archive and return the binary path.

    ``known_hash`` makes pooch check the archive against the pinned
    SHA-256: a mismatch raises instead of extracting, and a cached archive
    that no longer matches is downloaded again.
    """
    key = (platform.system(), platform.machine())
    if key not in _CLI_PLATFORM_MAP:
        raise RuntimeError(f"[Temporal install] Unsupported platform for CLI: {key}. " f"Supported: {sorted(_CLI_PLATFORM_MAP.keys())}")
    release_os, release_arch = _CLI_PLATFORM_MAP[key]
    pin = cli_pin()
    asset = pin["assets"][f"{release_os}_{release_arch}"]
    install_dir = _cache_dir() / pin["version"]

    extracted = pooch.retrieve(
        url=pin["base"].format(version=pin["version"]) + asset["file"],
        known_hash=f"sha256:{asset['sha256']}",
        path=install_dir,
        fname=asset["file"],
        processor=pooch.Unzip() if asset["file"].endswith(".zip") else pooch.Untar(),
        # requests' timeout is per-socket-read, not total download time —
        # only a 300s zero-byte stall aborts, so slow links can finish.
        # pooch's default is 30s, which killed npm installs on slow/WSL
        # links. progressbar must live on the downloader: retrieve()
        # ignores its own progressbar kwarg when downloader= is explicit.
        downloader=pooch.HTTPDownloader(timeout=300, progressbar=True),
    )

    target = _binary_name()
    match = next((Path(p) for p in extracted if Path(p).name == target), None)
    if match is None:
        raise RuntimeError(
            f"[Temporal install] CLI binary {target!r} not found in archive. " f"Extracted files: {[Path(p).name for p in extracted]}"
        )
    if platform.system() != "Windows":
        mode = match.stat().st_mode
        match.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return match


def marker_path(db_path: Path) -> Path:
    """The file beside the database that names its server and CLI."""
    return db_path.with_name(db_path.name + ".server.json")


def _read_marker(db_path: Path) -> dict | None:
    marker = marker_path(db_path)
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or _server_minor(value.get("server_version")) is None:
            raise ValueError("server version is missing")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Cannot verify Temporal server marker {marker}; existing database was preserved") from exc


def _binary_versions(binary: Path) -> dict[str, str]:
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True, timeout=15)
    match = re.search(r"(?:temporal CLI|temporal version)\s+(\d+\.\d+\.\d+)\s+\(Server\s+(\d+\.\d+\.\d+)", result.stdout, re.I)
    if match is None:
        raise RuntimeError(f"Cannot verify Temporal CLI/server version: {binary}")
    return {"cli_version": match[1], "server_version": match[2]}


def _find_binary(directory: Path) -> Path | None:
    candidates = sorted(directory.rglob(_binary_name())) if directory.is_dir() else []
    candidates = [path for path in candidates if path.is_file()]
    if len(candidates) > 1:
        raise RuntimeError(f"Ambiguous Temporal binaries in {directory}; database was preserved")
    return candidates[0] if candidates else None


def _existing_database_cli(db_path: Path) -> Path | None:
    """Keep the installed server/database pair; never download a replacement."""
    if not db_path.exists():
        return None
    marker = _read_marker(db_path)
    root = _cache_dir()
    if marker is None:
        os_, arch = _CLI_PLATFORM_MAP[(platform.system(), platform.machine())]
        archive = f"temporal_cli_latest_{os_}_{arch}." + ("zip.unzip" if os_ == "windows" else "tar.gz.untar")
        binary = _find_binary(root / archive)
        if binary is None:
            raise RuntimeError(f"No original Temporal binary is available for unmarked database {db_path}; "
                               "restore its binary or explicitly migrate the installation. Database was preserved")
        _binary_versions(binary)  # Prove it can identify the server before adoption.
        return binary
    version = marker.get("cli_version")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise RuntimeError(f"Cannot identify the original Temporal CLI for {db_path}; database was preserved")
    if version == cli_pin()["version"] and _server_minor(marker["server_version"]) == _server_minor(cli_pin()["server_version"]):
        return None  # Safe to install/reuse the same server minor's pinned binary.
    relative = marker.get("binary")
    binary = (root / relative).resolve() if isinstance(relative, str) else None
    if binary is not None and not binary.is_relative_to(root.resolve()):
        raise RuntimeError("Temporal binary marker escapes package directory; database was preserved")
    if binary is None or not binary.is_file():
        binary = _find_binary(root / str(version)) if version else None
    if binary is None:
        raise RuntimeError(f"The original Temporal {version} binary for {db_path} is missing; database was preserved")
    actual = _binary_versions(binary)
    if _server_minor(actual["server_version"]) != _server_minor(marker["server_version"]):
        raise RuntimeError("Temporal binary/server marker mismatch; database was preserved")
    return binary


def prepare_database(db_path: Path, binary: Path | None = None) -> None:
    """Validate before spawn; no database or WAL file is ever removed here."""
    pin = cli_pin()
    actual = _binary_versions(binary) if binary is not None else {
        "cli_version": pin["version"], "server_version": pin["server_version"],
    }
    marker = _read_marker(db_path) if db_path.exists() else None
    if db_path.exists() and marker is None and binary is None:
        raise RuntimeError(f"Unmarked Temporal database {db_path} requires its original binary; database was preserved")
    if marker is not None:
        if _server_minor(marker["server_version"]) != _server_minor(actual["server_version"]):
            raise RuntimeError(f"Temporal server minor mismatch for {db_path}; explicit migration required. Database was preserved")
        return
    record = {**actual, "created_at": datetime.now(timezone.utc).isoformat()}
    if binary is not None:
        try:
            record["binary"] = str(binary.resolve().relative_to(_cache_dir().resolve()))
        except ValueError:
            raise RuntimeError("Temporal binary must be inside its package directory; database was preserved") from None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids two startup attempts overwriting provenance.
    try:
        with marker_path(db_path).open("x", encoding="utf-8") as stream:
            json.dump(record, stream)
    except FileExistsError:
        existing = _read_marker(db_path)
        if existing is None or _server_minor(existing["server_version"]) != _server_minor(actual["server_version"]):
            raise RuntimeError("Concurrent Temporal database marker mismatch; database was preserved") from None


def _recorded_server(marker: Path) -> object:
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["server_version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _server_minor(version: object) -> str | None:
    if not isinstance(version, str):
        return None
    return ".".join(version.split(".")[:2]) if re.fullmatch(r"\d+\.\d+\.\d+", version) else None


__all__ = ["binary_installed", "cli_pin", "ensure_temporal_binaries", "marker_path", "prepare_database"]


def _main() -> int:
    """Standalone entry: ``python -m services.temporal._install``.

    Used by ``company build`` step [6/6] to materialise the Temporal
    CLI at build time instead of paying the download cost on first
    ``company start``. Fetches, verifies the binary exists on disk,
    prints the resolved location. Non-zero exit on any failure.
    """
    import sys as _sys

    try:
        paths = asyncio.run(ensure_temporal_binaries())
    except Exception as exc:  # noqa: BLE001 — propagate to non-zero exit
        print(f"[Temporal install] {exc}", file=_sys.stderr)
        return 1

    missing = [name for name, path in paths.items() if not path.exists()]
    if missing:
        print(
            f"[Temporal install] binaries missing after fetch: {missing}",
            file=_sys.stderr,
        )
        return 1

    for name, path in paths.items():
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
