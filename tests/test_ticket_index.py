from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from starlette.datastructures import QueryParams


pytest.importorskip("sqlalchemy")

from app import routes_ops, routes_requester
from app.ticket_index import (
    COMMON_TICKET_SORTS,
    DEFAULT_TICKET_SORT,
    OPS_DEFAULT_TICKET_STATES,
    REQUESTER_DEFAULT_TICKET_STATES,
    common_ticket_order_clauses,
    escaped_ilike_pattern,
    normalize_ticket_states,
    normalize_ticket_sort,
    split_query_items,
    statuses_for_ticket_states,
    ticket_state_for_status,
    ticket_state_query_items,
)


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _RecordingDb:
    def __init__(self, result_batches=()):
        self.statements = []
        self._result_batches = list(result_batches)

    def execute(self, statement):
        self.statements.append(statement)
        rows = self._result_batches.pop(0) if self._result_batches else []
        return _ScalarRows(rows)


def _request(**params):
    return SimpleNamespace(query_params=QueryParams(params))


def _requester_filters(**overrides):
    return {
        "q": "",
        "states": REQUESTER_DEFAULT_TICKET_STATES,
        "sort": DEFAULT_TICKET_SORT,
        "updated_since_viewed": False,
        **overrides,
    }


def _ops_filters(**overrides):
    return {
        "q": "",
        "states": OPS_DEFAULT_TICKET_STATES,
        "status": "",
        "route_target_id": "",
        "assigned_to": "",
        "urgent": False,
        "created_by_me": False,
        "needs_approval": False,
        "updated_since_viewed": False,
        "sort": DEFAULT_TICKET_SORT,
        **overrides,
    }


@pytest.mark.parametrize(
    ("sort_key", "expected"),
    [
        ("updated_desc", "tickets.updated_at DESC, tickets.reference_num DESC"),
        ("updated_asc", "tickets.updated_at ASC, tickets.reference_num ASC"),
        ("created_desc", "tickets.created_at DESC, tickets.reference_num DESC"),
        ("created_asc", "tickets.created_at ASC, tickets.reference_num ASC"),
    ],
)
def test_common_ticket_sorts_are_explicit_and_deterministic(sort_key, expected):
    assert ", ".join(str(clause) for clause in common_ticket_order_clauses(sort_key)) == expected


def test_ticket_sort_normalization_and_search_escaping_are_bounded_helpers():
    assert normalize_ticket_sort("created_asc", allowed=COMMON_TICKET_SORTS) == "created_asc"
    assert normalize_ticket_sort("updated_at desc; drop table", allowed=COMMON_TICKET_SORTS) == DEFAULT_TICKET_SORT
    assert escaped_ilike_pattern("100%_done\\") == "%100\\%\\_done\\\\%"


@pytest.mark.parametrize(
    ("sort_key", "expected_order"),
    [
        ("updated_desc", "tickets.updated_at DESC, tickets.reference_num DESC"),
        ("updated_asc", "tickets.updated_at ASC, tickets.reference_num ASC"),
        ("created_desc", "tickets.created_at DESC, tickets.reference_num DESC"),
        ("created_asc", "tickets.created_at ASC, tickets.reference_num ASC"),
        (
            "needs_reply_first",
            "CASE WHEN (tickets.status = :status_2) THEN :param_1 ELSE :param_2 END, tickets.updated_at DESC, tickets.reference_num DESC",
        ),
    ],
)
def test_requester_sorts_keep_owner_filter_and_deterministic_order(sort_key, expected_order):
    db = _RecordingDb()
    requester_id = uuid.uuid4()

    rows = routes_requester._load_requester_ticket_rows(
        db,
        requester_id=requester_id,
        filters=_requester_filters(sort=sort_key),
    )

    assert rows == []
    sql = " ".join(str(db.statements[0]).split())
    assert "tickets.created_by_user_id = :created_by_user_id_1" in sql
    assert f"ORDER BY {expected_order}" in sql


def test_requester_search_treats_sql_wildcards_as_literals():
    db = _RecordingDb()
    routes_requester._load_requester_ticket_rows(
        db,
        requester_id=uuid.uuid4(),
        filters=_requester_filters(q="100%_printer\\"),
    )

    compiled = db.statements[0].compile()
    assert any(value == r"%100\%\_printer\\%" for value in compiled.params.values())
    assert "ESCAPE '\\'" in str(compiled)


def test_requester_updated_filter_is_read_only_and_uses_existing_views():
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    requester_id = uuid.uuid4()
    ticket = SimpleNamespace(id=uuid.uuid4(), updated_at=now)
    view = SimpleNamespace(ticket_id=ticket.id, last_viewed_at=now + timedelta(minutes=1))
    db = _RecordingDb(([ticket], [view]))

    rows = routes_requester._load_requester_ticket_rows(
        db,
        requester_id=requester_id,
        filters=_requester_filters(updated_since_viewed=True),
    )

    assert rows == []
    assert len(db.statements) == 2


def test_requester_query_parser_normalizes_invalid_values_and_builds_canonical_url():
    parsed = routes_requester._read_requester_filters(
        _request(q="  printer  ", state="internal-only", sort="unsafe", updated_since_viewed="yes")
    )

    assert parsed == {
        "q": "printer",
        "states": REQUESTER_DEFAULT_TICKET_STATES,
        "sort": "updated_desc",
        "updated_since_viewed": True,
    }
    assert routes_requester._requester_list_url(parsed) == "/app/tickets?q=printer&updated_since_viewed=on"


def test_requester_return_path_is_strict_and_canonical():
    assert routes_requester._sanitize_requester_return_to(
        "/app/tickets?sort=created_asc&state=open&q=printer"
    ) == "/app/tickets?q=printer&sort=created_asc"
    assert routes_requester._sanitize_requester_return_to("https://evil.example/app/tickets") == "/app/tickets"
    assert routes_requester._sanitize_requester_return_to("/app/tickets?q=one&q=two") == "/app/tickets"
    assert routes_requester._sanitize_requester_return_to("/app/tickets?q=&q=two") == "/app/tickets"
    assert routes_requester._sanitize_requester_return_to("/app/tickets?unknown=") == "/app/tickets"
    assert routes_requester._sanitize_requester_return_to("/app/tickets?state=ai_triage") == "/app/tickets"
    assert routes_requester._sanitize_requester_return_to("/app/tickets?sort=unsafe") == "/app/tickets"


def test_requester_chips_preserve_sort_without_counting_it_as_a_filter():
    filters = _requester_filters(q="printer", sort="created_asc")

    assert routes_requester._requester_active_filter_items(filters) == [("q", "printer")]
    assert routes_requester._requester_filter_chips(filters) == [
        {
            "key": "q",
            "value": "printer",
            "remaining_url": "/app/tickets?sort=created_asc",
        }
    ]


def test_ops_legacy_unassigned_normalizes_and_explicit_assignment_wins():
    legacy = routes_ops._read_filters(_request(unassigned_only="on"))
    explicit = routes_ops._read_filters(_request(unassigned_only="on", assigned_to=str(uuid.uuid4())))

    assert legacy["assigned_to"] == "unassigned"
    assert "unassigned_only" not in legacy
    assert explicit["assigned_to"] != "unassigned"
    assert routes_ops._sanitize_ops_return_to("/ops?unassigned_only=on") == "/ops?assigned_to=unassigned"


def test_ops_sort_is_canonical_but_not_an_active_filter_or_chip():
    filters = _ops_filters(status="new", sort="urgent_first")

    assert ("sort", "urgent_first") in routes_ops._filter_query_items(filters)
    assert routes_ops._active_filter_query_items(filters) == [("status", "new")]
    assert [chip["key"] for chip in routes_ops._filter_chips(filters)] == ["status"]
    assert routes_ops._filter_chips(filters)[0]["remaining_query"] == "sort=urgent_first"


@pytest.mark.parametrize(
    ("sort_key", "expected_order"),
    [
        ("updated_desc", "tickets.updated_at DESC, tickets.reference_num DESC"),
        ("updated_asc", "tickets.updated_at ASC, tickets.reference_num ASC"),
        ("created_desc", "tickets.created_at DESC, tickets.reference_num DESC"),
        ("created_asc", "tickets.created_at ASC, tickets.reference_num ASC"),
        ("urgent_first", "tickets.urgent DESC, tickets.updated_at DESC, tickets.reference_num DESC"),
    ],
)
def test_ops_sorts_are_server_side_and_deterministic(sort_key, expected_order):
    db = _RecordingDb()
    rows = routes_ops._load_filtered_ticket_rows(
        db,
        current_user=SimpleNamespace(id=uuid.uuid4()),
        filters=_ops_filters(sort=sort_key),
    )

    assert rows == []
    sql = " ".join(str(db.statements[0]).split())
    assert f"ORDER BY {expected_order}" in sql


def test_ops_return_path_accepts_sort_and_rejects_invalid_values():
    assert routes_ops._sanitize_ops_return_to(
        "/ops/board?sort=urgent_first&urgent=on"
    ) == "/ops/board?urgent=on&sort=urgent_first"
    assert routes_ops._sanitize_ops_return_to("/ops?sort=unsafe") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops?urgent=maybe") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops?q=&q=printer") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops?unknown=") == "/ops/board"


def test_requester_and_ops_rows_remain_separate_privacy_boundaries():
    requester = Path("app/templates/requester_ticket_list_results.html").read_text(encoding="utf-8")
    ops = Path("app/templates/ops_ticket_rows.html").read_text(encoding="utf-8")

    assert "row.assignee" not in requester
    assert "route_target" not in requester
    assert "pending_draft" not in requester
    assert "row.assignee" in ops
    assert "route_target" in ops
    assert "pending_draft" in ops


def test_ticket_index_styles_keep_labels_accessible_and_bound_popovers_and_chips():
    ticket_list_css = Path("app/static/ticket-list.css").read_text(encoding="utf-8")
    board_css = Path("app/static/board.css").read_text(encoding="utf-8")
    app_css = Path("app/static/app.css").read_text(encoding="utf-8")

    mobile_label_rule = ticket_list_css.split(".ticket-index-row__mobile-label {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in mobile_label_rule
    assert "display: none" not in mobile_label_rule
    assert "clip-path: inset(50%)" in mobile_label_rule
    assert "inset-inline-end: 0" in board_css
    assert "inset-inline-start: auto" in board_css
    assert ".filter-chip {" in app_css
    assert "overflow-wrap: anywhere" in app_css


def test_ticket_state_groups_are_canonical_and_cover_raw_statuses():
    assert statuses_for_ticket_states(("waiting_reply",)) == (
        "new",
        "ai_triage",
        "waiting_on_dev_ti",
    )
    assert statuses_for_ticket_states(("waiting_user", "canceled")) == (
        "waiting_on_user",
        "canceled",
    )
    assert ticket_state_for_status("waiting_on_dev_ti") == "waiting_reply"
    assert ticket_state_for_status("canceled") == "canceled"


def test_ticket_state_selection_normalizes_order_and_uses_audience_defaults():
    assert normalize_ticket_states([], default=OPS_DEFAULT_TICKET_STATES) == (
        "waiting_reply",
        "waiting_user",
    )
    assert normalize_ticket_states([""], default=REQUESTER_DEFAULT_TICKET_STATES) == (
        "waiting_reply",
        "waiting_user",
    )
    assert normalize_ticket_states(
        ["resolved", "waiting_reply"],
        default=OPS_DEFAULT_TICKET_STATES,
    ) == ("waiting_reply", "resolved")
    assert ticket_state_query_items(
        REQUESTER_DEFAULT_TICKET_STATES,
        default=REQUESTER_DEFAULT_TICKET_STATES,
    ) == []


@pytest.mark.parametrize(
    "values",
    [
        ["unknown"],
        ["resolved", "resolved"],
        ["resolved", ""],
    ],
)
def test_ticket_state_selection_rejects_ambiguous_or_unknown_values(values):
    with pytest.raises(ValueError):
        normalize_ticket_states(values, default=OPS_DEFAULT_TICKET_STATES)


def test_query_split_allows_only_declared_repeated_keys():
    scalar, repeated = split_query_items(
        [("q", "printer"), ("state", "waiting_reply"), ("state", "resolved")],
        allowed_keys={"q", "state"},
        repeated_keys={"state"},
    )

    assert scalar == {"q": "printer"}
    assert repeated == {"state": ["waiting_reply", "resolved"]}

    with pytest.raises(ValueError):
        split_query_items(
            [("q", "first"), ("q", "second")],
            allowed_keys={"q", "state"},
            repeated_keys={"state"},
        )


@pytest.mark.parametrize("path", ["/ops", "/ops/board"])
def test_ops_defaults_and_explicit_single_state_survive_view_links(path):
    defaults = routes_ops._read_filters(SimpleNamespace(query_params=QueryParams("")))
    assert defaults["states"] == ("waiting_reply", "waiting_user")
    assert routes_ops._ops_view_url(path, defaults) == path

    explicit = routes_ops._read_filters(
        SimpleNamespace(query_params=QueryParams("state=waiting_reply"))
    )
    assert explicit["states"] == ("waiting_reply",)
    assert routes_ops._ops_view_url(path, explicit) == f"{path}?state=waiting_reply"


def test_routes_parse_repeated_states_and_keep_canonical_order():
    requester = routes_requester._read_requester_filters(
        SimpleNamespace(query_params=QueryParams("state=resolved&state=waiting_reply"))
    )
    ops = routes_ops._read_filters(
        SimpleNamespace(query_params=QueryParams("state=canceled&state=waiting_user"))
    )
    exact_ops = routes_ops._read_filters(
        SimpleNamespace(query_params=QueryParams("status=waiting_on_user"))
    )
    exact_ops_with_default_state = routes_ops._read_filters(
        SimpleNamespace(
            query_params=QueryParams("state=waiting_reply&status=waiting_on_user")
        )
    )
    exact_ops_with_invalid_state = routes_ops._read_filters(
        SimpleNamespace(query_params=QueryParams("state=bogus&status=resolved"))
    )
    legacy_requester = routes_requester._read_requester_filters(
        SimpleNamespace(query_params=QueryParams("state=waiting_on_user"))
    )

    assert requester["states"] == ("waiting_reply", "resolved")
    assert routes_requester._requester_list_url(requester) == (
        "/app/tickets?state=waiting_reply&state=resolved"
    )
    assert ops["states"] == ("waiting_user", "canceled")
    assert exact_ops["states"] == ("waiting_user",)
    assert routes_ops._ops_view_url("/ops", exact_ops) == "/ops?status=waiting_on_user"
    assert exact_ops_with_default_state["states"] == ("waiting_user",)
    assert routes_ops._ops_view_url("/ops", exact_ops_with_default_state) == (
        "/ops?status=waiting_on_user"
    )
    assert exact_ops_with_invalid_state["states"] == ("resolved",)
    assert routes_ops._ops_view_url("/ops/board", exact_ops_with_invalid_state) == (
        "/ops/board?status=resolved"
    )
    assert legacy_requester["states"] == ("waiting_user",)
    assert routes_requester._requester_list_url(legacy_requester) == (
        "/app/tickets?state=waiting_user"
    )


def test_default_ticket_queries_apply_the_expected_group_scope():
    requester_db = _RecordingDb()
    routes_requester._load_requester_ticket_rows(
        requester_db,
        requester_id=uuid.uuid4(),
        filters=_requester_filters(),
    )
    requester_params = requester_db.statements[0].compile().params

    ops_db = _RecordingDb()
    routes_ops._load_filtered_ticket_rows(
        ops_db,
        current_user=SimpleNamespace(id=uuid.uuid4()),
        filters=_ops_filters(),
    )
    ops_params = ops_db.statements[0].compile().params

    assert ["new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti"] in requester_params.values()
    assert ["new", "ai_triage", "waiting_on_user", "waiting_on_dev_ti"] in ops_params.values()


def test_canceled_status_migration_extends_and_safely_downgrades_constraint():
    source = Path(
        "shared/migrations/versions/20260904_0018_ticket_canceled_status.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260904_0018"' in source
    assert 'down_revision = "20260831_0017"' in source
    assert '"canceled"' in source
    assert "SET status = 'resolved'" in source


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_canceled_status_migration_uses_existing_constraint_name(direction):
    import importlib
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from shared.db import Base

    migration = importlib.import_module(
        "shared.migrations.versions.20260904_0018_ticket_canceled_status"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output, "target_metadata": Base.metadata},
    )
    with Operations.context(context):
        getattr(migration, direction)()
    sql = output.getvalue()
    assert "DROP CONSTRAINT ck_tickets_tickets_status" in sql
    assert "ADD CONSTRAINT ck_tickets_tickets_status CHECK" in sql
    assert "ck_tickets_ck_tickets" not in sql
