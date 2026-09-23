"""Authenticated admin web UI managing the ``groups`` table.

A single aiohttp application served from the same event loop as the bot
bridge, available only when ``CONFIG_SOURCE=database``. The page itself is a
static single-page shell (``src/web/static/``) — all data flows through the
JSON API under ``/api/*``, which requires a session cookie obtained via
``POST /api/login`` with the admin password.

Security posture (v1):

- Sessions are in-memory tokens (``secrets.token_urlsafe``), HttpOnly +
  SameSite=Strict cookies, 8h expiry; lost on restart.
- Passwords compared with ``secrets.compare_digest``; failed logins are
  rate-limited globally (5 failures / 10 min -> 60s lockout).
- Mutating routes require ``Content-Type: application/json`` — a cross-site
  form post cannot set it, which covers CSRF together with SameSite=Strict.
- Secrets are returned masked; an empty secret field on update keeps the
  stored value. Neither passwords, tokens, nor secret values are ever logged.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import asyncpg
from aiohttp import web

from .db_config import GroupDefaults, GroupRepository, GroupValidationError, validate_group_fields

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
_SESSION_COOKIE = "wfp_admin_session"
_SESSION_TTL_SECONDS = 8 * 3600.0
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 600.0
_LOGIN_LOCKOUT_SECONDS = 60.0

_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_JSON_CONTENT_TYPE = "application/json"

_PUBLIC_PATHS = ("/", "/api/login")


def mask_secret(value: str) -> str:
    """Mask a credential, keeping only its last 4 characters (8+ only)."""
    if not value:
        return ""
    if len(value) < 8:
        return "****"
    return f"****{value[-4:]}"


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def group_row_json(row: Mapping[str, Any], defaults: GroupDefaults) -> Dict[str, Any]:
    """One list row: masked secrets, raw + effective session values."""
    return {
        "id": row["id"],
        "name": row["name"],
        "wecom_robot_id": row.get("wecom_robot_id") or "",
        "wecom_robot_secret_masked": mask_secret(str(row.get("wecom_robot_secret") or "")),
        "dify_api_key_masked": mask_secret(str(row.get("dify_api_key") or "")),
        "session_max_total": row.get("session_max_total"),
        "session_ttl_seconds": row.get("session_ttl_seconds"),
        "effective_session_max_total": (
            row.get("session_max_total") or defaults.session_max_total
        ),
        "effective_session_ttl_seconds": (
            row.get("session_ttl_seconds") or defaults.session_ttl_seconds
        ),
        "enabled": row.get("enabled") is not False,
        "created_at": _iso_or_none(row.get("created_at")),
        "updated_at": _iso_or_none(row.get("updated_at")),
    }


class LoginRateLimiter:
    """Global failed-login counter: N failures in a window -> lockout."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: List[float] = []
        self._lockout_until = 0.0

    def _prune(self) -> None:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < _LOGIN_WINDOW_SECONDS]

    def blocked(self) -> bool:
        return self._clock() < self._lockout_until

    def record_failure(self) -> None:
        now = self._clock()
        self._failures.append(now)
        self._prune()
        if len(self._failures) >= _LOGIN_MAX_FAILURES:
            self._lockout_until = now + _LOGIN_LOCKOUT_SECONDS
            self._failures = []  # fresh count once the lockout expires

    def reset(self) -> None:
        self._failures = []
        self._lockout_until = 0.0


class AdminServer:
    """Serves the static admin page and the authenticated groups JSON API."""

    def __init__(
        self,
        *,
        repo: GroupRepository,
        password: str,
        request_reload: Callable[[], None],
        defaults: GroupDefaults,
        bind: str = "127.0.0.1",
        port: int = 8080,
        cookie_secure: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._password = password
        self._request_reload = request_reload
        self._defaults = defaults
        self._bind = bind
        self._port = port
        self._cookie_secure = cookie_secure
        self._clock = clock
        self._sessions: Dict[str, float] = {}  # token -> monotonic expiry
        self._limiter = LoginRateLimiter(clock)

        self.app = web.Application(
            middlewares=[self._error_middleware, self._auth_middleware]
        )
        router = self.app.router
        router.add_get("/", self._index)
        router.add_static("/static/", _STATIC_DIR)
        router.add_post("/api/login", self._login)
        router.add_post("/api/logout", self._logout)
        router.add_get("/api/groups", self._list_groups)
        router.add_post("/api/groups", self._create_group)
        router.add_put("/api/groups/{id}", self._update_group)
        router.add_patch("/api/groups/{id}/enabled", self._toggle_group)
        router.add_delete("/api/groups/{id}", self._delete_group)

        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """Bind the listener; raises OSError when the port is taken."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._bind, self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- middlewares -------------------------------------------------------

    @web.middleware
    async def _error_middleware(self, request, handler):
        """Turn unexpected failures into JSON 500s (no tracebacks to clients)."""
        try:
            return await handler(request)
        except (web.HTTPException, asyncio.CancelledError):
            raise
        except Exception:
            logger.warning("Admin request failed: %s %s", request.method, request.path)
            return _error_response(500, "internal error")

    @web.middleware
    async def _auth_middleware(self, request, handler):
        path = request.path
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await handler(request)

        # CSRF: a cross-site form post carries a non-JSON body content type.
        # Body-less mutations (DELETE, logout) have nothing to forge.
        if request.method in _MUTATING_METHODS and request.can_read_body:
            content_type = (
                request.headers.get("Content-Type") or ""
            ).split(";")[0].strip().lower()
            if content_type != _JSON_CONTENT_TYPE:
                return _error_response(415, "JSON content type required")

        token = request.cookies.get(_SESSION_COOKIE)
        if not self._session_valid(token):
            return _error_response(401, "unauthorized")
        return await handler(request)

    def _session_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        expiry = self._sessions.get(token)
        if expiry is None:
            return False
        if self._clock() >= expiry:
            self._sessions.pop(token, None)  # lazy prune
            return False
        return True

    # -- auth handlers ------------------------------------------------------

    async def _login(self, request) -> web.Response:
        body = await _json_body(request)
        if body is None:
            return _error_response(400, "JSON body required")

        if self._limiter.blocked():
            return _error_response(429, "too many attempts; retry later")

        password = body.get("password")
        if not isinstance(password, str) or not secrets.compare_digest(
            password.encode("utf-8"), self._password.encode("utf-8")
        ):
            self._limiter.record_failure()
            logger.warning("Admin login failed from %s", request.remote)
            return _error_response(401, "wrong password")

        self._limiter.reset()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = self._clock() + _SESSION_TTL_SECONDS
        response = web.Response(status=204)
        response.set_cookie(
            _SESSION_COOKIE,
            token,
            path="/",
            httponly=True,
            samesite="Strict",
            secure=self._cookie_secure,
            max_age=int(_SESSION_TTL_SECONDS),
        )
        return response

    async def _logout(self, request) -> web.Response:
        token = request.cookies.get(_SESSION_COOKIE)
        if token:
            self._sessions.pop(token, None)
        response = web.Response(status=204)
        response.del_cookie(_SESSION_COOKIE, path="/")
        return response

    # -- page -----------------------------------------------------------------

    async def _index(self, request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    # -- groups API -----------------------------------------------------------

    async def _list_groups(self, request) -> web.Response:
        rows = await self._repo.list_rows()
        payload = {
            "groups": [group_row_json(row, self._defaults) for row in rows],
            "defaults": {
                "session_max_total": self._defaults.session_max_total,
                "session_ttl_seconds": self._defaults.session_ttl_seconds,
            },
        }
        return web.json_response(payload)

    async def _create_group(self, request) -> web.Response:
        body = await _json_body(request)
        if body is None:
            return _error_response(400, "JSON body required")

        name = str(body.get("name") or "").strip()
        wecom_robot_id = str(body.get("wecom_robot_id") or "").strip()
        wecom_robot_secret = str(body.get("wecom_robot_secret") or "")
        dify_api_key = str(body.get("dify_api_key") or "")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            return _error_response(
                400, "enabled must be a boolean", {"enabled": "must be true or false"}
            )

        try:
            session_max_total = _optional_int(body.get("session_max_total"), "session_max_total")
            session_ttl_seconds = _optional_int(body.get("session_ttl_seconds"), "session_ttl_seconds")
            validate_group_fields(
                name=name,
                wecom_robot_id=wecom_robot_id,
                wecom_robot_secret=wecom_robot_secret,
                dify_api_key=dify_api_key,
                session_max_total=session_max_total,
                session_ttl_seconds=session_ttl_seconds,
            )
        except GroupValidationError as exc:
            return _error_response(400, "validation failed", {exc.field: str(exc)})

        try:
            await self._repo.insert_group(
                name=name,
                wecom_robot_id=wecom_robot_id,
                wecom_robot_secret=wecom_robot_secret,
                dify_api_key=dify_api_key,
                session_max_total=session_max_total,
                session_ttl_seconds=session_ttl_seconds,
                enabled=enabled,
            )
        except asyncpg.UniqueViolationError:
            return _error_response(409, f"group name already exists: {name}")

        self._request_reload()
        return web.Response(status=201)

    async def _update_group(self, request) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except ValueError:
            return _error_response(400, "invalid group id")

        body = await _json_body(request)
        if body is None:
            return _error_response(400, "JSON body required")

        rows = await self._repo.list_rows()
        current = next((row for row in rows if row["id"] == group_id), None)
        if current is None:
            return _error_response(404, "no such group")

        # Blank secret fields mean "keep the stored value".
        name = _value_or_current(body, "name", current)
        wecom_robot_id = _value_or_current(body, "wecom_robot_id", current)
        wecom_robot_secret = _value_or_current(body, "wecom_robot_secret", current)
        dify_api_key = _value_or_current(body, "dify_api_key", current)
        enabled = body.get("enabled")
        if enabled is None:
            enabled = current.get("enabled") is not False

        # Session params: explicit null clears (inherit global); absence keeps.
        try:
            if "session_max_total" in body:
                session_max_total = _optional_int(body.get("session_max_total"), "session_max_total")
            else:
                session_max_total = current.get("session_max_total")
            if "session_ttl_seconds" in body:
                session_ttl_seconds = _optional_int(body.get("session_ttl_seconds"), "session_ttl_seconds")
            else:
                session_ttl_seconds = current.get("session_ttl_seconds")
            validate_group_fields(
                name=name,
                wecom_robot_id=wecom_robot_id,
                wecom_robot_secret=wecom_robot_secret,
                dify_api_key=dify_api_key,
                session_max_total=session_max_total,
                session_ttl_seconds=session_ttl_seconds,
            )
        except GroupValidationError as exc:
            return _error_response(400, "validation failed", {exc.field: str(exc)})

        try:
            updated = await self._repo.update_group(
                group_id,
                name=name,
                wecom_robot_id=wecom_robot_id,
                wecom_robot_secret=wecom_robot_secret,
                dify_api_key=dify_api_key,
                session_max_total=session_max_total,
                session_ttl_seconds=session_ttl_seconds,
                enabled=enabled,
            )
        except asyncpg.UniqueViolationError:
            return _error_response(409, f"group name already exists: {name}")

        if not updated:
            return _error_response(404, "no such group")
        self._request_reload()
        return web.Response(status=204)

    async def _toggle_group(self, request) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except ValueError:
            return _error_response(400, "invalid group id")

        body = await _json_body(request)
        if body is None:
            return _error_response(400, "JSON body required")
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _error_response(
                400, "enabled must be a boolean", {"enabled": "must be true or false"}
            )

        updated = await self._repo.update_group(group_id, enabled=enabled)
        if not updated:
            return _error_response(404, "no such group")
        self._request_reload()
        return web.Response(status=204)

    async def _delete_group(self, request) -> web.Response:
        try:
            group_id = int(request.match_info["id"])
        except ValueError:
            return _error_response(400, "invalid group id")

        deleted = await self._repo.delete_group(group_id)
        if not deleted:
            return _error_response(404, "no such group")
        self._request_reload()
        return web.Response(status=204)


def _error_response(status: int, message: str, fields=None) -> web.Response:
    error: Dict[str, Any] = {"message": message}
    if fields:
        error["fields"] = fields
    return web.json_response({"error": error}, status=status)


async def _json_body(request) -> Optional[Dict[str, Any]]:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _value_or_current(body: Mapping[str, Any], key: str, current: Mapping[str, Any]) -> str:
    """Body value when a non-empty string was sent, else the stored one."""
    value = body.get(key)
    if value is not None:
        value = str(value).strip()
        if value:
            return value
    return str(current.get(key) or "")


def _optional_int(value: Any, field: str) -> Optional[int]:
    """None/null -> inherit; otherwise a positive-checkable int."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise GroupValidationError(field, f"{field} must be an integer or null") from None
