# Implementation Notes

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: htmx-ops-ui
- Phase Directory Key: htmx-ops-ui
- Phase Title: HTMX Ops Filters
- Scope: phase-local producer artifact

## Files Changed
- `app/routes_ops.py`
- `app/templates/base.html`
- `app/templates/ops_filters.html`
- `app/templates/ops_ticket_list.html`
- `app/templates/ops_board.html`
- `app/static/vendor/htmx.min.js`
- `tests/test_ops_workflow.py`

## Symbols Touched
- `_template_or_partial_response`
- `ops_ticket_list`
- `ops_board`

## Checklist Mapping
- C1: Vendored local HTMX asset at `app/static/vendor/htmx.min.js` and loaded from `base.html`.
- C2: `/ops` filter form now uses `hx-get`, `hx-target`, and `hx-push-url`; normal GET returns full template and HX GET returns `ops_ticket_rows.html`.
- C3: `/ops/board` uses the same HTMX form pattern with `ops_board_columns.html` as the HX fragment.
- C4: List and board routes still do not call `upsert_ticket_view`; existing read-tracking behavior remains detail-only and is covered by tests.

## Assumptions
- Stable fragment targets are implemented as wrapper div ids in the full-page templates, with HX swaps replacing only each wrapper's inner HTML.

## Preserved Invariants
- `/ops` and `/ops/board` remain GET-driven and retain non-JS filtering through the existing form action.
- `ops_ticket_rows.html` and `ops_board_columns.html` remain the canonical fragment templates.
- List and board filtering stays read-only with respect to `ticket_views.last_viewed_at`.

## Intended Behavior Changes
- Full list and board pages now load local HTMX and emit HTMX-enabled filter form attributes.
- HX requests receive fragment-only responses instead of the full page shell.

## Known Non-Changes
- No SPA behavior, no redesign beyond the minimal HTMX wiring, and no changes to ticket detail rendering or read tracking.

## Expected Side Effects
- Browser filter submissions with HTMX update only the rows/board-columns region and push the filtered URL into history.

## Validation Performed
- `pytest -q tests/test_ops_workflow.py`

## Deduplication / Centralization Decisions
- Reused the existing `_template_or_partial_response` helper for `/ops` instead of adding a list-specific branch.
