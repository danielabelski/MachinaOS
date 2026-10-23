"""The pinned Temporal CLI, and the database its dev server may open.

Downloads the official ``temporal`` CLI release named in
``server/config/temporal_cli.json`` (version, server version and each
asset's SHA-256) into ``<DATA_DIR>/packages/temporal/<version>/`` — the
same root that holds the Stripe and agent-browser binaries (see
:func:`core.paths.package_dir`). pooch verifies the archive against the
pinned hash before extracting it (zip on Windows, tar.gz elsewhere), so
a re-run is a cache hit and a tampered or truncated download fails.
Once the pinned binary is in place, every other file under
``packages/temporal/`` is deleted: older pinned versions and the
unpinned ``temporal_cli_latest_*`` archives earlier releases fetched.

:func:`prepare_database` keeps the dev server off a database another
server created. A marker beside ``temporal.db`` names the server that
created it; a database without one, or created by another server minor,
is deleted and the install starts on an empty Temporal database
(RFC-0005 E16 chose a fresh start over migrating Temporal state).

The downloaded ``temporal`` CLI powers ``temporal server start-dev``
(the SQLite dev server) and ad-hoc workflow / operator commands.
"""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
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

    The ``settings`` argument is accepted for API symmetry but unused —
    the version comes from ``server/config/temporal_cli.json``.
    """
    global _cached
    _ = settings

    async with _lock:
        if _cached is not None:
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
    _remove_other_clis(install_dir)
    return match


def _remove_other_clis(install_dir: Path) -> None:
    """Delete every file under ``packages/temporal/`` but the pinned version's.

    A file still in use (a dev server of another version left running) is
    logged and kept; the next start tries again.
    """
    for entry in install_dir.parent.iterdir():
        if entry == install_dir:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            logger.warning("[Temporal install] could not delete an old CLI file", path=str(entry), error=str(exc))
            continue
        logger.info("[Temporal install] deleted an old CLI file", path=str(entry))


def marker_path(db_path: Path) -> Path:
    """The file beside the database that names the server which created it."""
    return db_path.with_name(db_path.name + ".server.json")


def prepare_database(db_path: Path) -> None:
    """Delete the dev server's database unless the pinned server minor created it.

    A database without a marker (every one from before the pin), or with
    one naming another server minor, is deleted together with its WAL
    files, and the marker is rewritten for the pinned server, which then
    creates an empty database. A patch release of the same minor keeps
    the database. Runs before the dev server starts, never while it holds
    the file.
    """
    pin = cli_pin()
    marker = marker_path(db_path)
    if _server_minor(_recorded_server(marker)) == _server_minor(pin["server_version"]):
        return
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            path.unlink()
            logger.warning(
                "[Temporal install] deleted a database the pinned server did not create",
                path=str(path),
                server_version=pin["server_version"],
            )
    marker.write_text(
        json.dumps({"cli_version": pin["version"], "server_version": pin["server_version"]}),
        encoding="utf-8",
    )


def _recorded_server(marker: Path) -> object:
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["server_version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _server_minor(version: object) -> str | None:
    if not isinstance(version, str):
        return None
    return ".".join(version.split(".")[:2])


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
