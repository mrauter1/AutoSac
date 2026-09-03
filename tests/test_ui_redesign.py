from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from starlette.datastructures import QueryParams


pytest.importorskip("sqlalchemy")

from app import routes_ops
from app.ticket_live import build_ticket_live_state


def _empty_result():
    return SimpleNamespace(scalars=lambda: [])


def test_ops_filters_read_bounded_search_and_compile_literal_wildcards():
    request = SimpleNamespace(
        query_params=QueryParams(
            {
                "q": "  100%_printer  ",
                "urgent": "on",
            }
        )
    )
    filters = routes_ops._read_filters(request)
    statements = []

    class RecordingDb:
        def execute(self, statement):
            statements.append(statement)
            return _empty_result()

    rows = routes_ops._load_filtered_ticket_rows(
        RecordingDb(),
        current_user=SimpleNamespace(id=uuid.uuid4()),
        filters={
            **filters,
            "status": "",
            "route_target_id": "",
            "assigned_to": "",
            "unassigned_only": False,
            "created_by_me": False,
            "needs_approval": False,
            "updated_since_viewed": False,
        },
    )

    assert filters["q"] == "100%_printer"
    assert rows == []
    compiled = statements[0].compile()
    assert any(value == "%100\\%\\_printer%" for value in compiled.params.values())
    assert "lower(tickets.reference) LIKE lower(" in str(compiled)
    assert "lower(tickets.title) LIKE lower(" in str(compiled)


def test_ops_return_path_allows_only_queue_views_and_filter_keys():
    assert routes_ops._sanitize_ops_return_to("/ops/board?q=printer&urgent=on") == "/ops/board?q=printer&urgent=on"
    assert routes_ops._sanitize_ops_return_to("/ops?status=ai_triage") == "/ops?status=ai_triage"
    assert routes_ops._sanitize_ops_return_to("https://example.test/ops") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops/users") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops/board?admin=true") == "/ops/board"
    assert routes_ops._sanitize_ops_return_to("/ops/board?q=one&q=two") == "/ops/board"


def test_board_progressive_enhancement_contract_is_explicit():
    page = Path("app/templates/ops_board.html").read_text(encoding="utf-8")
    board = Path("app/templates/ops_board_columns.html").read_text(encoding="utf-8")
    client = Path("app/static/board.js").read_text(encoding="utf-8")

    assert 'method="post"' in board
    assert 'name="csrf_token"' in board
    assert 'name="return_to"' in board
    assert "data-board-move-form" in board
    assert "data-confirm" in board
    assert 'Accept: JSON_ACCEPT' in client
    assert 'headers: { "HX-Request": "true" }' in client
    assert "const mutationConfirmed" in client
    assert "const mutationRejected" in client
    assert "await this.reconcileBoard(reference)" in client
    assert "this.unlockCard(card)" in client
    assert client.index("requestBody = new FormData(form)") < client.index("this.lockCard(card)")
    assert 'querySelectorAll("[data-board-move-form] button:not(:disabled)")' in client
    assert 'querySelectorAll("button, input")' not in client
    assert "finePointer && !this.isCardLocked(card)" in client
    assert client.count("this.isCardLocked(card)") >= 4
    assert "button.dataset.disabledByBoardLock" in client
    assert 'this.root.addEventListener("click", this.onClick)' in client
    assert 'target.closest("[data-board-card]")' in client
    assert 'target.closest("a, button, input, select, textarea, summary, label, form, details, [contenteditable=\'true\']")' in client
    assert "event.button !== 0" in client
    assert "this.dragDidStart = true" in client
    assert 'card.querySelector(".board-card__open[href]")' in client
    assert "window.location.assign(openLink.href)" in client
    assert 'addEventListener("pointerup"' not in client
    assert "this.refreshLink.href = window.location.pathname + window.location.search" in client
    assert "data-board-refresh" in page
    assert "location.reload" not in client
    assert "sessionStorage" not in client
    assert board.count('href="/ops/tickets/new"') == 2
    assert board.count('t("button.open_ticket")') == 2
    assert '{% if status == "new" %}' in board
    assert board.index('{% if status == "new" %}') < board.index('class="ops-column__body"')


def test_ticket_templates_keep_composers_outside_live_fragments_and_audiences_separate():
    requester = Path("app/templates/requester_ticket_detail.html").read_text(encoding="utf-8")
    ops = Path("app/templates/ops_ticket_detail.html").read_text(encoding="utf-8")
    i18n = Path("app/i18n.py").read_text(encoding="utf-8")
    requester_fragments = Path("app/templates/requester_ticket_live_fragments.html").read_text(encoding="utf-8")
    ops_fragments = Path("app/templates/ops_ticket_live_fragments.html").read_text(encoding="utf-8")
    ticket_client = Path("app/static/ticket.js").read_text(encoding="utf-8")

    assert 'id="ticket-composer-region"' in requester
    assert 'data-ticket-composer' in requester
    assert 'id="ticket-composer-region"' in ops
    assert 'action="/ops/tickets/{{ ticket.reference }}/reply-public?' in ops
    assert 'action="/ops/tickets/{{ ticket.reference }}/note-internal?' in ops
    assert "data-composer-mode-panel=\"public\"" in ops
    assert "data-composer-mode-panel=\"internal\"" in ops
    assert ops.count("ticket-composer__form") == 2
    assert ops.count("ticket-composer__message-field") == 2
    assert ops.count('class="ticket-composer__footer"') == 2
    assert "audience-guidance--public" not in ops
    assert 'ops.detail.public_audience' not in ops
    assert '"ops.detail.public_audience"' not in i18n
    assert i18n.count('"ops.detail.public_reply_placeholder"') == 2
    assert i18n.count('"ops.detail.internal_note_placeholder"') == 2
    public_panel = ops.split('data-composer-mode-panel="public"', 1)[1].split(
        'data-composer-mode-panel="internal"', 1
    )[0]
    internal_panel = ops.split('data-composer-mode-panel="internal"', 1)[1]
    assert 'enctype="multipart/form-data"' in public_panel
    assert 'type="file" name="attachments" multiple' in public_panel
    assert 'aria-describedby="ops-public-attachment-hint"' in public_panel
    assert 'type="file"' not in internal_panel
    assert 'placeholder="{{ t(\'ops.detail.public_reply_placeholder\') }}"' in public_panel
    assert 'placeholder="{{ t(\'ops.detail.internal_note_placeholder\') }}"' in internal_panel
    assert public_panel.index("composer-advanced") < public_panel.index("ticket-composer__footer")
    assert "ticket-composer-region" not in requester_fragments
    assert "ticket-composer-region" not in ops_fragments
    assert "sessionStorage" not in ticket_client
    assert 'window.addEventListener("beforeunload"' in ticket_client
    assert "control.form !== form" in ticket_client
    assert "control.files.length > 0" in ticket_client


def test_provisional_ai_turn_is_not_a_persisted_message_identity():
    ledgers = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/templates/requester_ticket_ledger.html",
            "app/templates/ops_ticket_ledger.html",
        )
    )
    client = Path("app/static/ticket-live.js").read_text(encoding="utf-8")

    assert ledgers.count('id="ticket-live-provisional"') == 2
    assert 'id="ticket-message-{{ live_state' not in ledgers
    assert "awaitingCanonical" in client
    assert "data-ticket-live-provisional" in client
    assert 'activeElement.closest("#ticket-composer-region")' in client
    assert "currentComposerTop - composerViewportTop" in client
    assert client.index("this.applyState(payload, !payload.active && needsRefresh)") < client.index(
        "refreshWasHealthy = await this.refreshFragments(payload.content_version)"
    )


def test_live_run_key_is_stable_for_a_run_and_opaque_between_audiences():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    ticket = SimpleNamespace(
        id=uuid.uuid4(),
        reference="T-000059",
        title="Ticket",
        status="ai_triage",
        urgent=False,
        updated_at=now,
        assigned_to_user_id=None,
        route_target_id=None,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        created_at=now,
        started_at=now,
        last_heartbeat_at=now,
        ended_at=None,
        final_step_id=None,
    )
    router = SimpleNamespace(id=uuid.uuid4(), step_kind="router", status="running")
    specialist = SimpleNamespace(id=uuid.uuid4(), step_kind="specialist", status="running")

    first = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=router,
        audience="ops",
        stale_timeout_seconds=300,
        now=now,
    )
    later = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=specialist,
        audience="ops",
        stale_timeout_seconds=300,
        now=now,
    )
    requester = build_ticket_live_state(
        ticket=ticket,
        latest_run=run,
        latest_step=specialist,
        audience="requester",
        stale_timeout_seconds=300,
        now=now,
    )

    assert first.run_key == later.run_key
    assert first.run_key != str(run.id)
    assert requester.run_key != first.run_key


def test_page_specific_assets_and_reflow_rules_are_present():
    base = Path("app/templates/base.html").read_text(encoding="utf-8")
    app_css = Path("app/static/app.css").read_text(encoding="utf-8")
    board_css = Path("app/static/board.css").read_text(encoding="utf-8")
    ticket_css = Path("app/static/ticket.css").read_text(encoding="utf-8")
    ticket_new = Path("app/templates/requester_ticket_new.html").read_text(encoding="utf-8")
    requester_list = Path("app/templates/requester_ticket_list.html").read_text(encoding="utf-8")
    ops_detail = Path("app/templates/ops_ticket_detail.html").read_text(encoding="utf-8")
    ops_live_fragments = Path("app/templates/ops_ticket_live_fragments.html").read_text(encoding="utf-8")
    codex_turn_detail = Path("app/templates/ops_codex_turn_detail.html").read_text(encoding="utf-8")

    assert "block page_styles" in base
    assert 'aria-current="page"' in base
    assert 'href="/ops/tickets/new"' in base
    assert 'href="/app/tickets/new"' in base
    assert 'class="site-menu" open' in base
    assert 'class="site-menu__summary"' in base
    assert 'matchMedia("(max-width: 959px)")' in base
    assert "menu.open = !mobileMenu.matches" in base
    assert "padding-left: 216px" not in app_css
    assert "inset: 0 auto 0 0" not in app_css
    assert "max-width: 1680px" in app_css
    assert ".site-menu[open] {\n    width: 100%;" in app_css
    assert "grid-auto-columns: minmax(280px, 304px)" in board_css
    assert "overflow-x: auto" in board_css
    assert ".board-workspace" in board_css
    assert "grid-template-columns: minmax(0, 1fr)" in board_css
    assert "#ops-workspace-results" in board_css
    assert ".ticket-composer {\n  position: static;" in ticket_css
    assert ".page--ticket.page--ops-ticket-detail .ops-ticket-detail__header {\n  position: static;" in ticket_css
    assert ".page--ticket.page--ops-ticket-detail" in ticket_css
    assert "grid-template-columns: minmax(0, 57.5rem) auto" in ticket_css
    assert ".ops-ticket-detail__conversation" in ticket_css
    assert "max-width: 57.5rem" in ticket_css
    assert "max-width: 50rem" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ops-ticket-detail__thread {
  gap: 0.125rem;
  padding: 0;
}""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .message-card {
  width: fit-content;
  max-width: 96%;
  justify-self: start;
  margin: 0.125rem 0 0.25rem;""" in ticket_css
    assert "justify-self: start" in ticket_css
    assert "justify-self: end" in ticket_css
    assert "justify-self: stretch" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .timeline-status {
  width: 100%;
  max-width: none;
  justify-self: stretch;
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  justify-items: stretch;
  align-items: center;
  gap: 0.5rem;
  margin-block: 0;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .timeline-status__meta {
  flex-wrap: nowrap;
  justify-content: flex-start;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .timeline-status__text {
  min-width: 0;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-composer__footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-composer__footer-note {
  min-width: 10rem;
  flex: 1 1 12rem;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-composer .ticket-composer__attachment-field input[type="file"]:focus-visible {
  outline: 2px solid var(--focus);""" in ticket_css
    assert ".ops-ticket-inspector[open]" in ticket_css
    assert """.ops-ticket-inspector {
  min-width: 0;
  width: fit-content;
  transition: width 180ms ease;""" in ticket_css
    assert "@supports selector(::details-content) and (interpolate-size: allow-keywords)" in ticket_css
    assert "content-visibility 180ms allow-discrete" in ticket_css
    assert """@media (prefers-reduced-motion: reduce) {
  .page--ticket.page--ops-ticket-detail :where(
    .ops-ticket-inspector,
    .analysis-disclosure,
    .composer-advanced,
    .inspector-action
  )::details-content {
    transition: none;""" in ticket_css
    assert '<details class="ops-ticket-inspector" open>\n        <summary>' in ops_detail
    assert 't("ops.detail.inspector_toggle")' in ops_detail
    assert 'class="ops-ticket-inspector__content stack"' in ops_detail
    assert ops_detail.count('{% include "ops_ticket_header.html" %}') == 1
    main_index = ops_detail.index('class="ops-ticket-detail__main stack"')
    header_index = ops_detail.index('{% include "ops_ticket_header.html" %}')
    conversation_index = ops_detail.index('class="ops-ticket-detail__conversation stack"')
    ledger_index = ops_detail.index('{% include "ops_ticket_ledger.html" %}')
    composer_index = ops_detail.index('id="ticket-composer-region"')
    sidebar_index = ops_detail.index('class="ops-ticket-detail__sidebar"')
    assert main_index < header_index < conversation_index < ledger_index < composer_index < sidebar_index
    assert ops_detail.count('textarea name="body" rows="3"') == 2
    assert "ops-ticket-detail__conversation" not in ops_live_fragments
    assert "ops-ticket-inspector" not in ops_live_fragments
    assert "ops-ticket-inspector" not in codex_turn_detail
    assert "@media (max-width: 560px)" in ticket_css
    assert """.requester-detail .ticket-thread .message-card {
    max-width: 96%;
    padding-inline: 0.75rem;""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .timeline-status {
    grid-template-columns: minmax(0, 1fr);""" in ticket_css
    assert """.page--ticket.page--ops-ticket-detail .ticket-thread .timeline-status__meta {
    flex-wrap: wrap;""" in ticket_css
    assert "overflow-y: auto" not in ticket_css
    assert 'page--ticket-create' in ticket_new
    assert 'href="{{ ticket_list_path }}"' in ticket_new
    assert 'action="{{ ticket_create_action }}"' in ticket_new
    assert 'rel="stylesheet" href="/static/ticket.css"' in ticket_new
    assert 'input type="text" name="title"' in ticket_new
    assert 'maxlength="120"' in ticket_new
    assert 'textarea name="description" rows="8" required' in ticket_new
    assert 'input type="file" name="attachments" multiple' in ticket_new
    assert 'aria-describedby="ticket-create-attachment-hint"' in ticket_new
    assert 'input type="checkbox" name="urgent"' in ticket_new
    assert requester_list.count('href="{{ new_ticket_path }}"') == 2
    assert """.ticket-create {
  width: 100%;
  max-width: 50rem;
  margin-inline: auto;
}""" in ticket_css
    assert """.ticket-create__footer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;""" in ticket_css
