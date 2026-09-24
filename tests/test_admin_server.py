"""Tests for the admin web UI (auth, CRUD, masking, reload wiring).

The repository is a fake; the HTTP layer runs on aiohttp's TestServer. No
real database is involved.
"""

import asyncpg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from src.admin_server import AdminServer, LoginRateLimiter, group_row_json, mask_secret
from src.db_config import GroupDefaults

DEFAULTS = GroupDefaults(session_ttl_seconds=300, session_max_total=200)
DIFY_BASE_URL = "https://dify.example.test/v1"
PASSWORD = "secret-pw"


class FakeRepo:
    """In-memory stand-in for GroupRepository."""

    def __init__(self, rows=None, error=None):
        self.rows = []
        self.next_id = 1
        for row in rows or []:
            row = dict(row)
            row.setdefault("id", self.next_id)
            row.setdefault("enabled", True)
            self.next_id = max(self.next_id, row["id"] + 1)
            self.rows.append(row)
        self.error = error
        self.inserts = []
        self.updates = []

    async def list_rows(self):
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]

    async def insert_group(self, **fields):
        if self.error is not None:
            raise self.error
        if any(row["name"] == fields["name"] for row in self.rows):
            raise asyncpg.UniqueViolationError("duplicate key")
        row = {
            "id": self.next_id,
            "created_at": None,
            "updated_at": None,
            "enabled": fields.get("enabled", True),
            "session_max_total": fields.get("session_max_total"),
            "session_ttl_seconds": fields.get("session_ttl_seconds"),
            **{k: v for k, v in fields.items() if k not in ("enabled",)},
        }
        self.next_id += 1
        self.rows.append(row)
        self.inserts.append(fields)
        return row["id"]

    async def update_group(self, group_id, **fields):
        if self.error is not None:
            raise self.error
        new_name = fields.get("name")
        if new_name is not None and any(
            row["name"] == new_name for row in self.rows if row["id"] != group_id
        ):
            raise asyncpg.UniqueViolationError("duplicate key")
        for row in self.rows:
            if row["id"] == group_id:
                self.updates.append((group_id, fields))
                row.update(fields)
                return True
        return False

    async def delete_group(self, group_id):
        if self.error is not None:
            raise self.error
        for index, row in enumerate(self.rows):
            if row["id"] == group_id:
                del self.rows[index]
                return True
        return False


class ReloadRecorder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _make_server(repo=None, clock=None):
    repo = repo if repo is not None else FakeRepo()
    reload_recorder = ReloadRecorder()
    server = AdminServer(
        repo=repo,
        password=PASSWORD,
        request_reload=reload_recorder,
        defaults=DEFAULTS,
        dify_base_url=DIFY_BASE_URL,
        port=0,
        **({"clock": clock} if clock else {}),
    )
    return server, repo, reload_recorder


@pytest.fixture
async def client():
    server, repo, reload_recorder = _make_server()
    async with TestClient(TestServer(server.app)) as http:
        yield http, repo, reload_recorder


@pytest.fixture
async def authed_client():
    server, repo, reload_recorder = _make_server()
    async with TestClient(TestServer(server.app)) as http:
        response = await http.post(
            "/api/login", json={"password": PASSWORD}
        )
        assert response.status == 204
        yield http, repo, reload_recorder


# -- auth ------------------------------------------------------------------------


async def test_api_requires_auth_returns_401(client):
    http, _, _ = client
    response = await http.get("/api/groups")
    assert response.status == 401
    assert "error" in await response.json()


async def test_login_wrong_password_returns_401(client):
    http, _, _ = client
    response = await http.post("/api/login", json={"password": "nope"})
    assert response.status == 401


async def test_login_success_sets_cookie_and_grants_access(client):
    http, _, _ = client
    response = await http.post("/api/login", json={"password": PASSWORD})
    assert response.status == 204
    assert "wfp_admin_session" in http.session.cookie_jar.filter_cookies(
        URL("http://127.0.0.1")
    )

    response = await http.get("/api/groups")
    assert response.status == 200
    payload = await response.json()
    assert "groups" in payload and "defaults" in payload


async def test_logout_invalidates_session(client):
    http, _, _ = client
    await http.post("/api/login", json={"password": PASSWORD})
    response = await http.post("/api/logout")
    assert response.status == 204

    response = await http.get("/api/groups")
    assert response.status == 401


async def test_rate_limit_locks_out_after_five_failures(client):
    http, _, _ = client
    for _ in range(5):
        response = await http.post("/api/login", json={"password": "bad"})
        assert response.status == 401
    # Even the correct password is refused while locked out.
    response = await http.post("/api/login", json={"password": PASSWORD})
    assert response.status == 429


async def test_rate_limit_window_expires():
    class FakeClock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.record_failure()
    assert limiter.blocked()

    clock.now += 61  # past the 60s lockout
    assert not limiter.blocked()

    limiter.record_failure()  # fresh count after lockout
    assert not limiter.blocked()


async def test_mutating_route_rejects_non_json_content_type(authed_client):
    http, _, _ = authed_client
    response = await http.post(
        "/api/groups",
        data="name=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status == 415


async def test_login_password_not_logged(authed_client, caplog):
    http, _, _ = authed_client
    with caplog.at_level("WARNING"):
        await http.post("/api/login", json={"password": "wrong"})
    assert "wrong" not in caplog.text
    assert "secret-pw" not in caplog.text


# -- CRUD ------------------------------------------------------------------------


def _new_group_body(**overrides):
    body = {
        "name": "sales",
        "wecom_robot_id": "bot-1",
        "wecom_robot_secret": "sec-value",
        "dify_api_key": "app-value",
        "session_max_total": None,
        "session_ttl_seconds": None,
        "enabled": True,
    }
    body.update(overrides)
    return body


async def test_create_group_returns_201_and_persists(authed_client):
    http, repo, reload_recorder = authed_client
    response = await http.post("/api/groups", json=_new_group_body())
    assert response.status == 201
    assert repo.inserts[0]["name"] == "sales"
    assert repo.rows[0]["dify_api_key"] == "app-value"
    assert reload_recorder.calls == 1  # write triggered an immediate reload


async def test_create_validation_error_returns_field_messages(authed_client):
    http, repo, reload_recorder = authed_client
    response = await http.post("/api/groups", json=_new_group_body(name=" "))
    assert response.status == 400
    payload = await response.json()
    assert "name" in payload["error"]["fields"]
    assert reload_recorder.calls == 0


async def test_create_duplicate_name_returns_409(authed_client):
    http, _, _ = authed_client
    await http.post("/api/groups", json=_new_group_body())
    response = await http.post("/api/groups", json=_new_group_body())
    assert response.status == 409


async def test_update_blank_secret_keeps_existing_value(authed_client):
    http, repo, _ = authed_client
    await http.post("/api/groups", json=_new_group_body())
    group_id = repo.rows[0]["id"]

    response = await http.put(
        f"/api/groups/{group_id}",
        json=_new_group_body(
            wecom_robot_secret="", dify_api_key="", session_max_total=300
        ),
    )
    assert response.status == 204
    _, fields = repo.updates[0]
    assert fields["wecom_robot_secret"] == "sec-value"  # kept
    assert fields["dify_api_key"] == "app-value"  # kept
    assert fields["session_max_total"] == 300


async def test_update_nonblank_secret_overwrites(authed_client):
    http, repo, _ = authed_client
    await http.post("/api/groups", json=_new_group_body())
    group_id = repo.rows[0]["id"]

    response = await http.put(
        f"/api/groups/{group_id}",
        json=_new_group_body(dify_api_key="app-rotated"),
    )
    assert response.status == 204
    assert repo.rows[0]["dify_api_key"] == "app-rotated"


async def test_update_missing_id_returns_404(authed_client):
    http, _, _ = authed_client
    response = await http.put("/api/groups/999", json=_new_group_body())
    assert response.status == 404


async def test_update_session_null_clears_to_inherit(authed_client):
    http, repo, _ = authed_client
    await http.post("/api/groups", json=_new_group_body(session_max_total=300))
    group_id = repo.rows[0]["id"]

    response = await http.put(
        f"/api/groups/{group_id}", json=_new_group_body(session_max_total=None)
    )
    assert response.status == 204
    assert repo.rows[0]["session_max_total"] is None


async def test_toggle_enabled_patch(authed_client):
    http, repo, reload_recorder = authed_client
    await http.post("/api/groups", json=_new_group_body())
    group_id = repo.rows[0]["id"]

    response = await http.patch(
        f"/api/groups/{group_id}/enabled", json={"enabled": False}
    )
    assert response.status == 204
    assert repo.rows[0]["enabled"] is False
    assert reload_recorder.calls == 2  # create + toggle


async def test_delete_group_returns_204(authed_client):
    http, repo, reload_recorder = authed_client
    await http.post("/api/groups", json=_new_group_body())
    group_id = repo.rows[0]["id"]

    response = await http.delete(f"/api/groups/{group_id}")
    assert response.status == 204
    assert repo.rows == []
    assert reload_recorder.calls == 2  # create + delete


# -- list response shape ------------------------------------------------------------


async def test_list_masks_secrets_but_not_robot_id(authed_client):
    http, _, _ = authed_client
    await http.post(
        "/api/groups",
        json=_new_group_body(
            wecom_robot_secret="super-secret-value",
            dify_api_key="app-abcd1234",
        ),
    )
    response = await http.get("/api/groups")
    payload = await response.json()
    group = payload["groups"][0]

    assert group["wecom_robot_id"] == "bot-1"  # needed to manage the group
    assert group["wecom_robot_secret_masked"] == "****alue"
    assert group["dify_api_key_masked"] == "****1234"
    assert "super-secret-value" not in str(payload)
    assert "app-abcd1234" not in str(payload)


async def test_list_includes_effective_session_values(authed_client):
    http, _, _ = authed_client
    await http.post(
        "/api/groups", json=_new_group_body(session_ttl_seconds=600)
    )
    payload = await (await http.get("/api/groups")).json()
    group = payload["groups"][0]

    assert group["session_ttl_seconds"] == 600
    assert group["effective_session_ttl_seconds"] == 600
    assert group["session_max_total"] is None
    assert group["effective_session_max_total"] == 200
    assert payload["defaults"] == {
        "session_max_total": 200,
        "session_ttl_seconds": 300,
        "dify_base_url": DIFY_BASE_URL,
    }


# -- failure handling -------------------------------------------------------------


async def test_repository_error_returns_500_json(authed_client):
    server, repo, _ = _make_server(repo=FakeRepo(error=RuntimeError("db down")))
    async with TestClient(TestServer(server.app)) as http:
        await http.post("/api/login", json={"password": PASSWORD})
        response = await http.get("/api/groups")
        assert response.status == 500
        payload = await response.json()
        assert payload["error"]["message"] == "internal error"


async def test_failed_write_does_not_set_reload_event(authed_client):
    http, repo, reload_recorder = authed_client
    await http.post("/api/groups", json=_new_group_body())
    group_id = repo.rows[0]["id"]
    repo.error = RuntimeError("db down")

    response = await http.delete(f"/api/groups/{group_id}")
    assert response.status == 500
    assert reload_recorder.calls == 1  # only the create triggered a reload


# -- static assets ------------------------------------------------------------------


async def test_index_html_served(client):
    http, _, _ = client
    response = await http.get("/")
    assert response.status == 200
    body = await response.text()
    assert "wecom-forward-plus" in body


async def test_static_assets_served(client):
    http, _, _ = client
    for asset in ("app.js", "style.css"):
        response = await http.get(f"/static/{asset}")
        assert response.status == 200


# -- pure helpers --------------------------------------------------------------------


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("short") == "****"  # too short to reveal a tail
    assert mask_secret("super-secret-value") == "****alue"
    assert mask_secret("app-abcd1234") == "****1234"


def test_group_row_json_effective_values():
    row = {
        "id": 1,
        "name": "sales",
        "wecom_robot_id": "bot-1",
        "wecom_robot_secret": "sec-1",
        "dify_api_key": "app-1",
        "session_max_total": None,
        "session_ttl_seconds": None,
        "enabled": True,
        "created_at": None,
        "updated_at": None,
    }
    json_row = group_row_json(row, DEFAULTS)
    assert json_row["effective_session_max_total"] == 200
    assert json_row["effective_session_ttl_seconds"] == 300
    assert json_row["enabled"] is True
