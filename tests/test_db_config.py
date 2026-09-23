"""Tests for the pure mapping/validation/diff logic in db_config.

Rows are plain dicts (asyncpg Records behave like mappings); the repository
itself is not covered here — it is exercised end-to-end against a real
PostgreSQL during manual verification.
"""

import pytest

from src.config import GroupConfig
from src.db_config import (
    GroupDefaults,
    GroupValidationError,
    compute_group_diff,
    rows_to_desired,
    validate_group_fields,
)

DEFAULTS = GroupDefaults(session_ttl_seconds=300, session_max_total=200)


def _row(**overrides):
    row = {
        "id": 1,
        "name": "sales",
        "wecom_robot_id": "bot-1",
        "wecom_robot_secret": "sec-1",
        "dify_api_key": "app-1",
        "session_max_total": None,
        "session_ttl_seconds": None,
        "enabled": True,
    }
    row.update(overrides)
    return row


def _group(name="sales", **overrides):
    group = GroupConfig(
        index=1,
        name=name,
        wecom_robot_id="bot-1",
        wecom_robot_secret="sec-1",
        dify_api_key="app-1",
        session_max_total=200,
    )
    for key, value in overrides.items():
        setattr(group, key, value)
    return group


# -- rows_to_desired -----------------------------------------------------------


def test_row_to_desired_maps_required_fields():
    desired = rows_to_desired(
        [_row(session_max_total=300, session_ttl_seconds=600)], DEFAULTS
    )
    group = desired["sales"]
    assert group.index == 1
    assert group.name == "sales"
    assert group.wecom_robot_id == "bot-1"
    assert group.wecom_robot_secret == "sec-1"
    assert group.dify_api_key == "app-1"
    assert group.session_max_total == 300
    assert group.session_ttl_seconds == 600


def test_nullable_session_fields_fall_back_to_defaults():
    desired = rows_to_desired([_row()], DEFAULTS)
    assert desired["sales"].session_max_total == 200
    assert desired["sales"].session_ttl_seconds is None  # inherit at pool build


def test_disabled_rows_are_excluded():
    rows = [_row(name="a"), _row(id=2, name="b", enabled=False)]
    desired = rows_to_desired(rows, DEFAULTS)
    assert set(desired) == {"a"}


def test_empty_table_is_legal():
    assert rows_to_desired([], DEFAULTS) == {}


def test_invalid_row_error_names_field_and_group_but_not_values():
    with pytest.raises(GroupValidationError) as excinfo:
        rows_to_desired([_row(dify_api_key="")], DEFAULTS)
    message = str(excinfo.value)
    assert "dify_api_key" in message
    assert "sales" in message
    assert "sec-1" not in message
    assert "app-1" not in message


def test_nonpositive_session_values_rejected():
    with pytest.raises(GroupValidationError, match="session_max_total"):
        rows_to_desired([_row(session_max_total=0)], DEFAULTS)
    with pytest.raises(GroupValidationError, match="session_ttl_seconds"):
        rows_to_desired([_row(session_ttl_seconds=-1)], DEFAULTS)


def test_duplicate_names_rejected():
    rows = [_row(name="a"), _row(id=2, name="a")]
    with pytest.raises(GroupValidationError, match="duplicate"):
        rows_to_desired(rows, DEFAULTS)


# -- validate_group_fields ------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["name", "wecom_robot_id", "wecom_robot_secret", "dify_api_key"],
)
def test_blank_required_fields_rejected(field):
    kwargs = {
        "name": "sales",
        "wecom_robot_id": "bot",
        "wecom_robot_secret": "sec",
        "dify_api_key": "app",
        "session_max_total": None,
        "session_ttl_seconds": None,
    }
    kwargs[field] = " "
    with pytest.raises(GroupValidationError) as excinfo:
        validate_group_fields(**kwargs)
    assert excinfo.value.field == field


def test_name_length_capped():
    with pytest.raises(GroupValidationError) as excinfo:
        validate_group_fields(
            name="x" * 101,
            wecom_robot_id="bot",
            wecom_robot_secret="sec",
            dify_api_key="app",
            session_max_total=None,
            session_ttl_seconds=None,
        )
    assert excinfo.value.field == "name"


def test_valid_fields_pass():
    validate_group_fields(
        name="sales",
        wecom_robot_id="bot",
        wecom_robot_secret="sec",
        dify_api_key="app",
        session_max_total=300,
        session_ttl_seconds=600,
    )


# -- compute_group_diff ---------------------------------------------------------


def test_diff_added():
    diff = compute_group_diff(live=[], current={}, desired={"sales": _group()})
    assert [g.name for g in diff.added] == ["sales"]
    assert not diff.removed and not diff.restarted and not diff.updated


def test_diff_removed():
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired={})
    assert diff.removed == ["sales"]


def test_diff_credential_change_requires_restart():
    desired = {"sales": _group(wecom_robot_secret="sec-2")}
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired=desired)
    assert [g.name for g in diff.restarted] == ["sales"]
    assert not diff.updated
    assert not diff.added


def test_diff_robot_id_change_requires_restart():
    desired = {"sales": _group(wecom_robot_id="bot-9")}
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired=desired)
    assert [g.name for g in diff.restarted] == ["sales"]


def test_diff_dify_key_only_is_soft_update():
    desired = {"sales": _group(dify_api_key="app-rotated")}
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired=desired)
    assert [g.name for g in diff.updated] == ["sales"]
    assert not diff.restarted


def test_diff_session_param_only_is_soft_update():
    desired = {"sales": _group(session_max_total=999, session_ttl_seconds=60)}
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired=desired)
    assert [g.name for g in diff.updated] == ["sales"]
    assert not diff.restarted


def test_diff_identical_is_empty():
    desired = {"sales": _group()}
    diff = compute_group_diff(live=["sales"], current={"sales": _group()}, desired=desired)
    assert not diff


def test_diff_live_without_current_is_added():
    # A connect failure dropped the client but the store still knows it; the
    # group must be re-added (retried), not silently skipped.
    diff = compute_group_diff(live=[], current={"sales": _group()}, desired={"sales": _group()})
    assert [g.name for g in diff.added] == ["sales"]
    assert not diff.restarted
