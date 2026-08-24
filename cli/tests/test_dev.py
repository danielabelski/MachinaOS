"""Smoke tests for ``cli.commands.dev``."""

from __future__ import annotations

from pathlib import Path

from cli.commands import dev
from cli.config import Config, load_config


def _cfg() -> Config:
    # Use the real env-file loader so test config mirrors production
    # behaviour (``.env.template`` -> ``.env`` -> ``os.environ``).
    # No hardcoded values: ``.env.template`` is the single source of
    # truth, same as ``cli.commands.start``/``dev`` at runtime.
    return load_config()


def test_has_vite_false_when_missing(tmp_path: Path):
    assert dev._has_vite(tmp_path) is False


def test_has_vite_true_when_in_root_node_modules(tmp_path: Path):
    (tmp_path / "node_modules" / "vite").mkdir(parents=True)
    assert dev._has_vite(tmp_path) is True


def test_has_vite_true_when_in_client_node_modules(tmp_path: Path):
    (tmp_path / "client" / "node_modules" / "vite").mkdir(parents=True)
    assert dev._has_vite(tmp_path) is True


def test_dev_command_force_flag_defaults_false():
    # The Vite dep cache must survive normal boots -- an unconditional
    # wipe forced a full esbuild re-optimize (minutes on Windows) on
    # every first page load. ``--force`` is the explicit opt-in and maps
    # to Vite's own ``optimizeDeps.force`` via the VITE_FORCE env var.
    import inspect

    params = inspect.signature(dev.dev_command).parameters
    assert "force" in params
    assert params["force"].default.default is False  # typer.Option wrapper


def test_build_specs_force_sets_vite_force_env(tmp_path: Path):
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=True, force=True)
    client = next(s for s in specs if s.name == "client")
    assert client.env.get("VITE_FORCE") == "1"


def test_build_specs_default_does_not_set_vite_force_env(tmp_path: Path):
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=True)
    client = next(s for s in specs if s.name == "client")
    assert "VITE_FORCE" not in client.env


def test_build_specs_dev_uses_vite_when_available(tmp_path: Path):
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=True)
    by_name = {s.name: s for s in specs}
    assert by_name["client"].argv[:3] == ["bun", "run", "client:start"]


def test_build_specs_dev_without_vite_is_backend_only(tmp_path: Path):
    # No Vite => no client process at all: the backend serves the built
    # SPA itself (SERVE_STATIC_CLIENT in server/main.py, on by default).
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=False)
    assert {s.name for s in specs} == {"server"}


def test_build_specs_daemon_binds_0_0_0_0(tmp_path: Path):
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=True, use_vite=True)
    server_argv = next(s for s in specs if s.name == "server").argv
    assert "0.0.0.0" in server_argv


def test_build_specs_non_daemon_binds_127_0_0_1(tmp_path: Path):
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=True)
    server_argv = next(s for s in specs if s.name == "server").argv
    assert "127.0.0.1" in server_argv


def test_build_specs_dev_has_no_temporal_spec(tmp_path: Path):
    # The Temporal dev server is backend-owned: started from the FastAPI
    # lifespan (services.temporal._runtime.ensure_started) when
    # TEMPORAL_ENABLED and the configured address is loopback.
    cfg = _cfg()
    specs = dev._build_specs(tmp_path, cfg, daemon=False, use_vite=True)
    assert not any(s.name == "temporal" for s in specs)
