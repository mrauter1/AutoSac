from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest


pytest.importorskip("sqlalchemy")

from app.ticket_live import build_ticket_live_state, if_none_match_matches, ticket_live_representation_etag


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def _ticket(*, status: str = "ai_triage", updated_at: datetime = NOW):
    return SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000059",
        title="Live ticket",
        status=status,
        urgent=False,
        updated_at=updated_at,
        assigned_to_user_id=None,
        route_target_id=None,
    )


def _run(*, status: str = "running", heartbeat_at: datetime | None = NOW):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        created_at=NOW - timedelta(minutes=2),
        started_at=NOW - timedelta(minutes=1),
        last_heartbeat_at=heartbeat_at,
        ended_at=None,
        final_step_id=None,
    )


def _step(*, kind: str, status: str = "running", index: int = 1):
    return SimpleNamespace(id=uuid.uuid4(), step_kind=kind, status=status, step_index=index)


@pytest.mark.parametrize(
    ("run_status", "step", "expected_phase"),
    (
        ("pending", None, "queued"),
        ("running", None, "preparing"),
        ("running", _step(kind="router"), "routing"),
        ("running", _step(kind="selector"), "selecting_specialist"),
        ("running", _step(kind="specialist"), "analyzing"),
        ("running", _step(kind="router", status="succeeded"), "preparing"),
        ("running", _step(kind="selector", status="succeeded"), "preparing"),
        ("running", _step(kind="specialist", status="succeeded"), "finalizing"),
        ("running", _step(kind="specialist", status="human_review"), "finalizing"),
        ("running", _step(kind="specialist", status="failed"), "preparing"),
    ),
)
def test_ops_live_state_maps_durable_run_and_step_phases(run_status, step, expected_phase):
    state = build_ticket_live_state(
        ticket=_ticket(),
        latest_run=_run(status=run_status),
        latest_step=step,
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert state.active is True
    assert state.phase == expected_phase
    assert state.delayed is False


def test_terminal_run_wins_over_stale_active_step():
    state = build_ticket_live_state(
        ticket=_ticket(),
        latest_run=_run(status="succeeded"),
        latest_step=_step(kind="specialist"),
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert state.active is False
    assert state.phase == "idle"


def test_stale_heartbeat_maps_to_taking_longer_without_marking_failure():
    state = build_ticket_live_state(
        ticket=_ticket(),
        latest_run=_run(heartbeat_at=NOW - timedelta(minutes=6)),
        latest_step=_step(kind="specialist"),
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert state.active is True
    assert state.phase == "taking_longer"
    assert state.delayed is True


def test_requester_projection_is_coarse_and_hides_internal_status_runs():
    active_run = _run()
    requester_state = build_ticket_live_state(
        ticket=_ticket(),
        latest_run=active_run,
        latest_step=_step(kind="selector"),
        audience="requester",
        stale_timeout_seconds=300,
        now=NOW,
    )
    hidden_state = build_ticket_live_state(
        ticket=_ticket(status="waiting_on_dev_ti"),
        latest_run=active_run,
        latest_step=_step(kind="specialist"),
        audience="requester",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert requester_state.active is True
    assert requester_state.phase == "working"
    assert hidden_state.active is False
    assert hidden_state.phase == "idle"


def test_fresh_heartbeat_does_not_continuously_invalidate_state_or_content():
    ticket = _ticket()
    run = _run(heartbeat_at=NOW - timedelta(seconds=30))
    original_step = _step(kind="specialist")
    first = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=original_step,
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )
    run.last_heartbeat_at = NOW
    second = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=original_step,
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert first.version == second.version
    assert first.content_version == second.content_version


def test_step_progress_changes_state_without_requiring_full_content_refresh():
    ticket = _ticket()
    run = _run()
    router_state = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=_step(kind="router"),
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )
    specialist_state = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=_step(kind="specialist", index=2),
        audience="ops",
        stale_timeout_seconds=300,
        now=NOW,
    )

    assert router_state.version != specialist_state.version
    assert router_state.content_version == specialist_state.content_version


def test_etag_matching_accepts_lists_and_wildcard():
    assert if_none_match_matches('"old", "current"', etag='"current"') is True
    assert if_none_match_matches("*", etag='"current"') is True
    assert if_none_match_matches('"old"', etag='"current"') is False
    assert if_none_match_matches(None, etag='"current"') is False


def test_live_representation_etag_varies_by_locale():
    english = ticket_live_representation_etag(version="state-v1", ui_locale="en")
    portuguese = ticket_live_representation_etag(version="state-v1", ui_locale="pt-BR")

    assert english != portuguese
    assert english.startswith('"') and english.endswith('"')


def test_live_client_uses_conditional_serial_fragment_updates_without_page_reload():
    source = Path("app/static/ticket-live.js").read_text(encoding="utf-8")
    requester_fragments = Path("app/templates/requester_ticket_live_fragments.html").read_text(encoding="utf-8")
    ops_fragments = Path("app/templates/ops_ticket_live_fragments.html").read_text(encoding="utf-8")

    assert 'headers["If-None-Match"]' in source
    assert 'redirect: "manual"' in source
    assert "new Set([301, 302, 303, 307, 308])" in source
    assert 'response.type === "opaqueredirect"' in source
    assert "REDIRECT_STATUSES.has(response.status)" in source
    assert "if (this.pendingContentVersion && this.pendingContentVersion !== this.contentVersion)" in source
    assert "await this.refreshFragments(this.pendingContentVersion)" in source
    assert "this.pendingContentVersion = payload.content_version" in source
    assert "refreshWasHealthy = await this.refreshFragments" in source
    assert "if (refreshWasHealthy === true)" in source
    assert "this.contentVersion === requestedVersion" in source
    assert source.index("this.pendingContentVersion = expectedVersion") < source.index("if (this.refreshing)")
    assert "if (this.pendingContentVersion === expectedVersion)" in source
    assert 'this.root.addEventListener("htmx:afterRequest", onAfterRequest)' in source
    assert 'this.root.removeEventListener("htmx:afterRequest", onAfterRequest)' in source
    assert "event.detail.successful === true" in source
    assert "source: this.root" in source
    assert "completed without an HTMX completion event" in source
    assert "return null" in source
    assert "return false" in source
    assert "this.stopLiveUpdates()" in source
    assert "this.elapsedTimer = null" in source
    assert "document.hidden" in source
    assert "this.polling" in source
    assert 'target: "#ticket-ledger-region"' in source
    assert 'headers: { "X-AutoSac-Live-Refresh": "true" }' in source
    assert "location.reload" not in source
    assert "ticket-composer-region" not in requester_fragments
    assert "ticket-composer-region" not in ops_fragments
