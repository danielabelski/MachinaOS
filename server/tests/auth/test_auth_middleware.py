"""Tests for AuthMiddleware, including an invariant that pins its riskiest rule.

The middleware gates by exclusion: any GET/HEAD outside `/api/` and `/ws/` is
served without authentication, because the SPA shell must load before login.
That is correct today only because every router in the app is mounted under
`/api/` (or is already deliberately public). Nothing enforced that, so a future
router mounted at, say, `/admin/` would become world-readable with no code
review signal. `TestPublicSurfaceInvariant` is that signal.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from middleware.auth import PUBLIC_PATHS, PUBLIC_PREFIXES, AuthMiddleware
from tests.auth.conftest import make_settings

pytestmark = pytest.mark.unit

# Prefixes the middleware actually gates. Anything outside these is public for
# GET/HEAD by the exclusion rule at middleware/auth.py:49.
GATED_PREFIXES = ("/api/", "/ws/")


@pytest.fixture
def overridden_container():
    """Override the DI providers the middleware resolves, then restore.

    Two traps here, both of which made every gating assertion pass vacuously
    by leaving auth disabled (the real `.env` ships VITE_AUTH_ENABLED=false):

      1. It must use dependency_injector's `override()` API. A plain
         `monkeypatch.setattr` on the container does not take.
      2. It must override the container object `middleware.auth` itself
         holds. That module does `from core.container import container` at
         import time, and this suite's conftest wipes `core.*` from
         sys.modules -- so `core.container` may be a *different* module
         object than the one the middleware closed over, depending on
         whether the middleware was imported before or after the wipe.
    """
    import middleware.auth as middleware_module

    container = middleware_module.container

    applied = []

    def _apply(*, settings, user_auth):
        container.settings.override(settings)
        container.user_auth_service.override(user_auth)
        applied.extend([container.settings, container.user_auth_service])

    yield _apply

    for provider in applied:
        provider.reset_override()


def _build_app(user_auth, settings, overridden_container):
    """App with AuthMiddleware plus a couple of probe routes."""
    overridden_container(settings=settings, user_auth=user_auth)

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    # `request` MUST be annotated: an untyped parameter is treated as a
    # required query parameter, and the resulting 422 masks the status the
    # middleware actually produced.
    @app.get("/api/protected")
    async def protected(request: Request):  # pragma: no cover - exercised via HTTP
        return {
            "user_id": getattr(request.state, "user_id", None),
            "is_owner": getattr(request.state, "is_owner", None),
        }

    @app.get("/")
    async def spa_shell():  # pragma: no cover - exercised via HTTP
        return {"shell": True}

    @app.post("/webhook/incoming")
    async def webhook():  # pragma: no cover - exercised via HTTP
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestGating:
    async def test_api_route_without_cookie_is_401(self, user_auth, settings, overridden_container):
        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.get("/api/protected")
        assert response.status_code == 401

    async def test_valid_cookie_populates_request_state(self, user_auth, settings, overridden_container):
        user, _ = await user_auth.register(
            email="owner@example.com", password="hunter2hunter2", display_name="Owner"
        )
        token = user_auth.create_access_token(user)

        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.get(
                "/api/protected", cookies={settings.jwt_cookie_name: token}
            )

        assert response.status_code == 200
        assert response.json()["user_id"] == str(user.id)

    async def test_garbage_cookie_is_401(self, user_auth, settings, overridden_container):
        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.get(
                "/api/protected", cookies={settings.jwt_cookie_name: "not-a-jwt"}
            )
        assert response.status_code == 401

    async def test_auth_disabled_passes_through_anonymously(self, user_auth, overridden_container):
        settings = make_settings(vite_auth_enabled="false")
        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.get("/api/protected")

        assert response.status_code == 200
        assert response.json()["is_owner"] is True

    async def test_spa_shell_is_public(self, user_auth, settings, overridden_container):
        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.get("/")
        assert response.status_code == 200

    async def test_webhook_prefix_bypasses_the_cookie_gate(self, user_auth, settings, overridden_container):
        client = _build_app(user_auth, settings, overridden_container)
        async with client as http:
            response = await http.post("/webhook/incoming")
        assert response.status_code == 200


class TestPublicIngressPaths:
    """Plugin routes a provider calls without a session cookie, published with
    ``register_public_ingress_path``. Each route authenticates the request
    itself; Discord's interactions endpoint verifies Ed25519 over the body."""

    def test_discord_interactions_are_public_and_its_callback_is_not(self):
        import nodes.discord  # noqa: F401 -- registers its public ingress path

        middleware = AuthMiddleware(app=FastAPI())
        assert middleware._is_public_path("/api/discord/interactions")
        assert middleware._is_public_path("/api/discord/interactions/account-1")
        assert not middleware._is_public_path("/api/discord/interactionsX")
        assert not middleware._is_public_path("/api/discord/callback")

    async def test_a_post_without_a_cookie_reaches_the_route(self, user_auth, settings, overridden_container):
        import nodes.discord  # noqa: F401 -- registers its public ingress path

        overridden_container(settings=settings, user_auth=user_auth)
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.post("/api/discord/interactions")
        async def interactions():  # pragma: no cover - exercised via HTTP
            return {"reached": True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post("/api/discord/interactions")
        assert response.status_code == 200


class TestPublicSurfaceInvariant:
    """Every mounted router must sit under a gated prefix, or be explicitly
    public. Without this, the GET-exclusion rule turns any new non-`/api`
    router into an unauthenticated endpoint."""

    @staticmethod
    def _all_routers():
        """Every APIRouter the app mounts: the `routers` package plus the
        plugin routers registered on `import nodes`."""
        import importlib
        import pkgutil

        from fastapi import APIRouter

        import routers as routers_pkg

        found: list[tuple[str, APIRouter]] = []

        for info in pkgutil.iter_modules(routers_pkg.__path__):
            module = importlib.import_module(f"routers.{info.name}")
            for attr in vars(module).values():
                if isinstance(attr, APIRouter):
                    found.append((f"routers.{info.name}", attr))

        import nodes  # noqa: F401  -- registers plugin routers as a side effect
        from services.ws_handler_registry import get_routers

        for entry in get_routers():
            router = entry[0] if isinstance(entry, tuple) else entry
            if isinstance(router, APIRouter):
                found.append(("plugin", router))

        return found

    def test_every_router_is_gated_or_explicitly_public(self):
        offenders = []
        for source, router in self._all_routers():
            prefix = router.prefix or ""
            if prefix.startswith(GATED_PREFIXES):
                continue
            if prefix and prefix.rstrip("/") + "/" in PUBLIC_PREFIXES:
                continue
            for route in router.routes:
                path = getattr(route, "path", "")
                full = f"{prefix}{path}"
                if full.startswith(GATED_PREFIXES):
                    continue
                if full in PUBLIC_PATHS:
                    continue
                if full.startswith(PUBLIC_PREFIXES):
                    continue
                offenders.append(f"{source}: {full}")

        assert not offenders, (
            "These routes sit outside the gated prefixes and are not in the public "
            "allowlist, so AuthMiddleware's GET/HEAD exclusion rule serves them "
            "unauthenticated:\n  " + "\n  ".join(sorted(offenders))
        )

    def test_gated_prefixes_match_the_middleware_source(self):
        """If the middleware's exclusion tuple changes, this test's notion of
        'gated' must change with it -- otherwise the invariant above silently
        checks the wrong thing."""
        import inspect

        source = inspect.getsource(AuthMiddleware.dispatch)
        assert 'path.startswith(("/api/", "/ws/"))' in source
