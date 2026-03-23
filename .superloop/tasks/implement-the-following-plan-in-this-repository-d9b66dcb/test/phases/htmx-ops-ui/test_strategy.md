# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: htmx-ops-ui
- Phase Directory Key: htmx-ops-ui
- Phase Title: HTMX Ops Filters
- Scope: phase-local producer artifact

## Behavior To Coverage Map

- AC-1 normal GET `/ops`:
  - `test_ops_list_route_returns_full_template_for_normal_get`
  - Covers full-page shell, local HTMX asset load, stable list target id, and preserved non-JS `method="get"` / `action="/ops"` form fallback.
- AC-1 normal GET `/ops/board`:
  - `test_ops_board_route_returns_full_template_for_normal_get`
  - Covers full-page shell, local HTMX asset load, stable board target id, and preserved non-JS `method="get"` / `action="/ops/board"` form fallback.
- AC-2 HX fragment `/ops`:
  - `test_ops_list_route_returns_rows_fragment_for_hx_request`
  - Covers rows-only response shape by asserting the page shell, filter form, and target wrapper are absent while the rows fragment content remains present.
- AC-2 HX fragment `/ops/board`:
  - `test_ops_board_route_returns_columns_fragment_for_hx_request`
  - Covers board-columns-only response shape by asserting the page shell, filter form, and target wrapper are absent while fragment content remains present.
- AC-2 template wiring:
  - `test_ops_routes_source_and_templates_keep_internal_and_public_lanes_separate`
  - Covers local HTMX script inclusion, HTMX form attributes, and stable wrapper ids in the list and board templates.
- AC-3 preserved read tracking:
  - `test_ops_list_route_does_not_mark_ticket_as_read`
  - `test_ops_board_route_does_not_mark_ticket_as_read`
  - `test_ops_detail_route_marks_ticket_as_read`
  - Covers the invariant that list/board filtering stays read-only while detail views still update `ticket_views`.

## Edge Cases / Failure Paths

- Wrong-role browser access remains `403` for list, board, and detail routes.
- Unauthenticated browser access still redirects to `/login` with a sanitized `next` parameter.
- Empty filter results are used for deterministic fragment assertions to avoid ordering or timestamp flake.

## Stabilization Notes

- Route tests monkeypatch `_ops_filter_context` to a fixed empty result so assertions do not depend on database state, sorting, or timestamps.
- Read-tracking assertions monkeypatch `upsert_ticket_view` and count invocations directly instead of inspecting persisted timestamps.

## Known Gaps

- No browser-executed HTMX integration test is added in this phase; coverage stays server-side and template-contract focused.
