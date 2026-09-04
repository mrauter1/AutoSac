# Ticket list and queue UI/UX redesign plan

**Status:** implementation-ready plan; no runtime changes are included

**Baseline:** `ui-overhaul` at `bc3955d`, inspected 2026-09-03

**Scope:** requester ticket index (`GET /app` and `GET /app/tickets`), Ops ticket queue (`GET /ops`), and the shared Ops filter contract used by `GET /ops/board`

## 1. Decision summary

Bring **My tickets / Meus tickets** and **Ticket queue / Fila de tickets** to the same compact standard as the Board and ticket-detail work without creating a second frontend architecture.

The implementation should:

1. Replace large stacked cards and decorative page-header panels with compact, aligned ticket rows and a flat workspace header.
2. Keep the requester and Ops templates separate so an Ops-only field cannot leak through a generic shared template. Share CSS vocabulary and query helpers, not authorization-sensitive markup.
3. Keep important controls visible:
   - requester: search, requester-safe state, order, and **updated since my last view**;
   - Ops: search, status, assignee, order, urgent, needs approval, and updated since last view;
   - Ops secondary disclosure: route target and created by me.
4. Remove the duplicate visible Ops controls for “unassigned”: use the existing assignee selector as the one canonical control while accepting legacy `unassigned_only` URLs defensively.
5. Add a small allow-list of named server-side sort modes. Never accept a column name or SQL direction from the request.
6. Make the query string authoritative. Filters and order must survive HTMX updates, browser history, Board/List switching, ticket navigation, locale switching, and explicit back links.
7. Preserve progressive enhancement: every control remains a normal GET form or link; HTMX only replaces the bounded results region and pushes the canonical URL.
8. Add no database migration, saved-filter persistence, frontend framework, bulk-action model, custom priority score, or client-side sorting.

This is a presentation and read-model improvement. Ticket visibility, `TicketView` read semantics, status transitions, AI behavior, and mutations remain unchanged.

## 2. Current-state findings

### 2.1 Requester list

Observed in `app/routes_requester.py` and `app/templates/requester_ticket_list.html`:

- The route loads only the current user's tickets and orders them by `Ticket.updated_at DESC`.
- There is no search, state filter, update filter, result count, order control, URL query contract, or partial response.
- Each ticket is a large generic card containing reference, public status, urgency, unseen-update marker, title, and last-update time.
- The list correctly does not mark tickets as viewed. Only the detail route updates `TicketView`.
- The **Open a ticket** action is already role-aware.
- Opening a ticket and using the explicit **My tickets** back link loses any future list query state unless a safe return path is added.

### 2.2 Ops queue

Observed in `app/routes_ops.py`, `app/templates/ops_filters.html`, `app/templates/ops_ticket_rows.html`, and `app/static/board.css`:

- Ops already has server-side search and filters for status, route target, assignee, urgent, unassigned, created by me, pending approval, and updated since viewed.
- Search, status, assignee, and most task-critical flags are hidden behind a disclosure unless a filter is already active.
- `assigned_to=unassigned` and `unassigned_only=on` express the same concept and can be selected together, producing a contradictory query.
- Ordering is fixed to `Ticket.updated_at DESC` and lacks a deterministic tie-breaker.
- Board and List share the same filter parser/context, query-preserving view switch, HTMX target, and ticket return URL. This is a useful contract and should remain shared.
- Pending-approval and updated-since-viewed filtering occurs after the primary ticket query because those signals come from related projections. Sorting must not silently depend on this in-memory filtering.
- Rows are large cards with metadata spread across several vertical paragraphs, so comparison across tickets is slow.
- The detail return path already preserves allow-listed Ops filters, but it does not yet know about `sort`.

### 2.3 Styling and architecture

- `app.css` still gives generic ticket cards, buttons, inputs, and card headers generous padding, large radii, gradients, and shadows.
- `board.css` owns the Ops filter toolbar because the toolbar is shared by Board and List.
- A page-scoped stylesheet can improve both index pages without weakening the separate requester and Ops projections.
- The existing Jinja + HTMX pattern is sufficient. No SPA or client state store is justified.

## 3. UX model

### 3.1 Desktop composition

Requester:

```text
My tickets                                             [Open a ticket]
─────────────────────────────────────────────────────────────────────
[Search reference or title____] [State ▾] [Order ▾] [□ Updated] [Apply]
12 tickets                                                   [Reset]
─────────────────────────────────────────────────────────────────────
T-000123  Printer fails after login       Waiting for your reply  Sep 03
          [Urgent] [Updated]
─────────────────────────────────────────────────────────────────────
T-000118  VPN access request              Waiting on team          Sep 02
```

Ops:

```text
Ticket queue                         [Open a ticket] [Board | List]
─────────────────────────────────────────────────────────────────────
[Search________] [Status ▾] [Assigned to ▾] [Order ▾] [Apply]
[□ Urgent] [□ Needs approval] [□ Updated] [More filters ▾]
24 matching tickets  [active filter chips]                 [Reset]
─────────────────────────────────────────────────────────────────────
Ticket                         State/attention       People/route       Updated
T-000123 Printer fails…        AI Triage · Urgent   Ana → Carlos        Sep 03
T-000118 VPN access…           Waiting on team      Bruno · Network     Sep 02
```

The sketches define hierarchy, not exact pixels. Final values should follow the compact ticket-detail treatment already in `ticket.css`: restrained borders, small radii, limited shadows, 0.75–0.875rem supporting type, and controls no smaller than a 2rem visual target.

### 3.2 Responsive behavior

- At wide widths, rows use stable CSS Grid columns so reference/title, state, ownership, and time align vertically.
- Below the row breakpoint, the same semantic DOM becomes a two- or three-line layout. Do not render a second mobile template.
- Filter controls wrap into two columns, then one column near 560px. Search occupies the full first row on narrow screens.
- Active chips scroll or wrap; they never force horizontal page overflow.
- Supporting labels remain available to screen readers and become visible on mobile where column position no longer communicates meaning.
- Preserve visible focus, logical DOM order, 320px reflow, and EN/pt-BR text expansion.

## 4. Shared visual language, separate projections

Add `app/static/ticket-list.css` and load it only on the requester and Ops list pages. Keep `board.css` loaded on the Ops list because it owns the shared Ops toolbar; keep Board-only column/card rules in `board.css`.

Recommended page and component classes:

```text
page--ticket-index
page--requester-ticket-index
page--ops-ticket-index
ticket-index
ticket-index__header
ticket-index__controls
ticket-index__summary
ticket-index__list
ticket-index__columns
ticket-index-row
ticket-index-row__primary
ticket-index-row__state
ticket-index-row__people
ticket-index-row__updated
```

Keep `requester_ticket_list.html` and `ops_ticket_rows.html` separate. Both use the class vocabulary, but each explicitly renders only its authorized fields:

| Area | Requester row | Ops row |
| --- | --- | --- |
| Primary | reference and title | reference and title |
| State | requester-safe status | internal workflow status and route target |
| Attention | urgent, updated | urgent, updated, pending approval |
| People | none | requester and assignee |
| Time | last updated | last updated |
| Destination | requester detail | Ops detail with safe return URL |

Do not create a universal Jinja row partial that decides visibility from missing values. Separate templates are a cheap, strong privacy boundary. Sharing presentation classes is sufficient to prevent visual drift.

Rows remain one primary link with no nested interactive controls. Use a semantic list and real `<time datetime="…">` values. State and attention must be expressed with text as well as color.

## 5. Filter contracts

### 5.1 Requester query contract

Add a small parser in `routes_requester.py` for:

| Parameter | Allowed values | Default | Meaning |
| --- | --- | --- | --- |
| `q` | trimmed text, maximum 120 characters | empty | escaped case-insensitive reference/title contains search |
| `state` | `open`, `waiting_on_user`, `resolved` | empty/all | requester-safe status bucket |
| `updated_since_viewed` | existing accepted booleans | false | only tickets changed since this requester viewed them |
| `sort` | requester sort allow-list | `updated_desc` | named server ordering |

Requester state behavior:

- empty: all owned tickets, preserving current default behavior;
- `open`: every status except `resolved`;
- `waiting_on_user`: exact internal status, labelled **Waiting for your reply / Aguardando sua resposta**;
- `resolved`: exact resolved status.

Do not expose duplicate requester options for `new` and `ai_triage`, because both intentionally present as **Reviewing / Em análise**. Do not expose Ops-only route, assignee, approval, or internal-state concepts.

The requester form is fully visible; it does not need an advanced disclosure. Render result count and removable active-filter chips. Show Reset when either a filter or a non-default sort is active.

### 5.2 Ops query contract

Keep the current dictionary-based route contract; do not introduce a generic filter framework or persistence model.

Always-visible controls:

- `q`
- `status`
- `assigned_to`
- `sort`
- `urgent`
- `needs_approval`
- `updated_since_viewed`
- Apply and Reset

Secondary disclosure:

- `route_target_id`
- `created_by_me`

The disclosure opens when one of its own fields is active. Its badge counts secondary filters only; the overall result summary/chips still represents every active constraint.

Use `assigned_to` as the single visible assignment grammar:

- empty = anyone;
- `unassigned` = no assignee;
- current user's UUID = render first as **Me / Eu**;
- another valid Ops user UUID = that user.

Compatibility rule for old URLs:

- Keep `unassigned_only` in the accepted/sanitized legacy key set during this release.
- If `unassigned_only` is true and `assigned_to` is empty, normalize to `assigned_to=unassigned`.
- If both are present, the explicit `assigned_to` value wins and the legacy boolean is ignored.
- Generated forms, chips, view links, and return URLs emit only the canonical `assigned_to` form.
- Remove the `unassigned_only` checkbox from the template, but do not break old bookmarks abruptly.

### 5.3 Canonical URL and HTMX behavior

- Default/empty values are omitted from generated URLs.
- Sort is part of URL state but is not counted or rendered as a filter chip; its visible select already communicates it.
- Separate “query items” from “active filter items” in Ops helpers so adding sort cannot corrupt chip counts.
- Invalid enum/sort values normalize to safe defaults. A malformed assignee UUID yields an empty result, preserving the current defensive behavior; it never broadens the query or raises an exception.
- Forms use ordinary GET. HTMX uses the same form, swaps the bounded result wrapper, and sets `hx-push-url="true"`.
- Give controls stable IDs so HTMX can preserve focus. Announce the updated result count through a concise `role="status"`/`aria-live="polite"` region.
- Board/List switching retains the same normalized filters and sort.
- On Board, sort changes ordering *inside each status column only*. It never changes workflow-column order or introduces manual rank.
- Locale links retain the canonical current list query.

## 6. Ordering contract

Create a small `app/ticket_index.py` helper for the genuinely shared mechanics only:

- default sort key;
- common sort-key normalization;
- escaped ILIKE contains pattern;
- deterministic common SQLAlchemy order clauses.

Audience filter parsing and presentation stay in their route modules. Do not add a broad `FilterState` abstraction.

Common sort keys:

| Key | UI label | SQL ordering |
| --- | --- | --- |
| `updated_desc` | Recently updated | `updated_at DESC, reference_num DESC` |
| `updated_asc` | Least recently updated | `updated_at ASC, reference_num ASC` |
| `created_desc` | Newest created | `created_at DESC, reference_num DESC` |
| `created_asc` | Oldest created | `created_at ASC, reference_num ASC` |

Requester-only option:

| Key | UI label | SQL ordering |
| --- | --- | --- |
| `needs_reply_first` | Needs my reply first | `waiting_on_user` first, then `updated_at DESC, reference_num DESC` |

Ops-only option:

| Key | UI label | SQL ordering |
| --- | --- | --- |
| `urgent_first` | Urgent first | `urgent DESC, updated_at DESC, reference_num DESC` |

Rules:

- Current behavior remains the default.
- Every ordering is deterministic.
- Map allow-listed names to explicit clauses. Never pass a request string to `getattr`, `text`, `order_by`, or raw SQL.
- Do not call any ordering **priority**, **SLA**, or **attention** until the product has a defined policy and authoritative fields for that meaning.
- Keep ordering in SQL before existing projection enrichment. Do not sort on pending-draft or unseen state in Python.

## 7. Navigation continuity

### 7.1 Ops

Extend the existing Ops query/return contract to include normalized `sort`:

- `_OPS_FILTER_QUERY_KEYS`
- query serialization
- Board/List view URLs
- ticket links
- `_sanitize_ops_return_to`

The existing allow-listed-path, query-length, duplicate-key, and external-URL defenses remain mandatory.

### 7.2 Requester

Add the minimum equivalent needed to avoid losing the new list state:

1. Generate a canonical requester list URL from `q`, `state`, `updated_since_viewed`, and non-default `sort`.
2. Put that URL in a URL-encoded `return_to` on ticket-detail links.
3. Sanitize `return_to` to the canonical `/app/tickets` path, allow-listed keys/values, bounded query length, and no scheme, host, fragment, or duplicate keys.
4. Use the sanitized URL for the detail header's **My tickets** link.
5. Carry it through requester reply and resolve forms and their redirects, including validation rerenders.
6. Include it in the requester live-detail fragment URL so a polling refresh cannot silently reset the header back link.

Do not add session storage or custom scroll restoration in this phase. Browser Back already restores exact navigation state in normal cases; the explicit back link should preserve the query but may return to the top of the list.

## 8. Template changes

### `requester_ticket_list.html`

- Load `ticket-list.css`.
- Apply the requester ticket-index page classes.
- Convert the decorative header card to a flat compact header with **Open a ticket**.
- Add a visible GET/HTMX filter form and result summary.
- Move rows into a replaceable `requester_ticket_list_results.html` fragment.
- Render compact requester-only row fields and query-preserving detail links.
- Distinguish unfiltered empty state (**No tickets yet**, include create CTA) from filtered no-match state (explain filters and include Reset).

### `ops_ticket_list.html` and `ops_ticket_rows.html`

- Load `ticket-list.css` alongside `board.css`.
- Apply Ops ticket-index page classes.
- Use a flat header with **Open a ticket** and retain the Board/List switch in the control area.
- Render an optional desktop column guide and compact Ops rows.
- Keep the ticket link's existing safe `return_to` behavior.
- Distinguish an empty queue from zero filtered matches.

### `ops_filters.html`

- Recompose the existing shared form; do not duplicate its filter logic in the List template.
- Keep important controls visible and move only secondary controls into the disclosure.
- Remove the visible `unassigned_only` checkbox.
- Add sort, stable control IDs, compact labels, active summary, and reset affordance.
- Preserve normal form behavior and the existing bounded HTMX target.

### Requester detail templates

- Replace the hard-coded `/app/tickets` header link with the sanitized requester list URL.
- Add hidden return context to reply/resolve forms.
- Ensure live fragments receive the same URL.

## 9. Route and presenter changes

### Requester

- Replace `_ticket_list_rows(db, requester_id=…)` with an explicitly named query function accepting normalized requester list state.
- Preserve owner filtering as the first invariant.
- Escape `%`, `_`, and `\` for contains search, matching the proven Ops behavior.
- Load `TicketView` only for candidate owned tickets and retain current unseen calculation.
- Filter `updated_since_viewed` without writing a view record.
- Return rows, result count, normalized query state, chips/links, canonical list URL, and whether any non-default filter is active.
- Return the full template normally and the requester results fragment only for HTMX requests from the list form.

### Ops

- Extend `_read_filters`, query serialization, chip construction, `_ops_filter_context`, and the safe return contract for sort and canonical assignment.
- Apply explicit order clauses in `_load_filtered_ticket_rows`.
- Preserve current pending-draft, user, route-target, and unseen projections.
- Preserve the Board's fixed status columns and only change row order within groups.

## 10. Localization

Add EN and pt-BR keys together for:

- order field and every sort option;
- requester state field and its buckets;
- updated-only requester control;
- **Me / Eu** in assignment;
- **More filters / Mais filtros**;
- filtered-empty versus truly-empty text;
- compact column labels where existing row labels are insufficient.

Reuse existing keys for search, status, assignee, urgency, needs approval, updated since viewed, reset, result count, requester, assigned user, last update, Open a ticket, and Board/List.

No user-facing string belongs in Python, CSS, or JavaScript unless it already follows the established server-provided localization pattern.

## 11. Implementation sequence

### Phase 1 — Characterize contracts

1. Add tests for current owner/Ops authorization, “list does not mark viewed,” full versus HTMX responses, view switching, and return-path safety.
2. Add failing tests for sort normalization, deterministic ties, wildcard escaping, query propagation, legacy unassigned normalization, and invalid values.
3. Capture local synthetic screenshots at 390, 768, 1024, and 1440px in EN and pt-BR.

### Phase 2 — Query state and ordering

1. Add the bounded shared ticket-index query helpers.
2. Extend Ops parsing/serialization/order/return paths without changing templates first.
3. Add requester query parsing, filters, ordering, canonical URLs, and partial response.
4. Add requester return-path propagation through detail, reply, resolve, and live refresh.

Exit condition: route tests prove identical default results/order to the current implementation and correct behavior for every new allow-listed value.

### Phase 3 — Compact presentation

1. Add `ticket-list.css` and page-specific classes.
2. Recompose the requester header, filters, summary, empty states, and rows.
3. Recompose the Ops header, shared filter toolbar, summary, empty states, and rows.
4. Verify Board filter layout and card ordering because `ops_filters.html` is shared.

### Phase 4 — Accessibility and visual verification

1. Verify keyboard-only filter, reset, view-switch, row-open, detail-back, and browser-history flows.
2. Verify focus retention and result announcements after HTMX swaps.
3. Test 320px reflow, 400% zoom, long titles, missing assignee/route, all attention flags, empty data, filtered empty, and large result sets.
4. Compare EN and pt-BR screenshots at target viewports.
5. Run the complete automated suite and `git diff --check`.

## 12. Test plan

### Query/unit tests

- each allowed requester and Ops sort emits the intended ordered clauses;
- equal timestamps resolve deterministically by `reference_num`;
- invalid/missing sorts use `updated_desc`;
- search remains bounded and treats `%`, `_`, and `\` literally;
- requester state buckets map to the specified statuses;
- requester ownership remains mandatory under every query combination;
- `updated_since_viewed` remains a read-only projection;
- canonical assignment handles anyone, me/current UUID, unassigned, another valid user, malformed UUID, and legacy combinations;
- sort survives query generation but does not increase active-filter counts or create a filter chip.

### Route/template tests

- requester and Ops full pages contain the compact header, visible primary controls, selected order, count, and correct rows;
- HTMX requests return only the correctly identified wrapper and preserve the selected values;
- no JavaScript still supports applying and clearing every filter;
- Board/List links preserve normalized filter/order state;
- Ops ticket links and requester ticket links carry safe return state;
- detail back links, requester validation errors, reply redirects, resolve redirects, and live fragments preserve safe requester state;
- malicious/external/oversized/duplicate/unknown return values fall back safely;
- requester HTML never contains assignee, route, internal status, approval, or other Ops-only projections;
- empty and no-match states are distinct and provide the correct recovery action;
- EN and pt-BR render every new key without raw identifiers.

### Manual/browser checks

- 390, 768, 1024, and 1440px;
- mouse, keyboard, and touch-sized viewport;
- browser Back/Forward after multiple filters and sorts;
- HTMX failure leaves the last server-rendered view understandable and ordinary form submission remains available;
- long Portuguese labels do not overlap or truncate essential meaning;
- focus is visible and not lost after a swap;
- Board controls and drag/drop remain unchanged except for intentionally visible filters and selected within-column ordering.

## 13. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Generic reuse leaks Ops data to requester | Separate authorized route projections and templates; share CSS only; add negative HTML assertions. |
| Sort becomes an injection surface | Named allow-list mapped to explicit SQLAlchemy clauses; invalid values fall back. |
| Adding sort breaks active-filter chips | Separate canonical query serialization from active-filter serialization. |
| Shared Ops filter template regresses Board | Board route/fragment/browser checks in the same change; sort affects cards within columns only. |
| Old unassigned bookmarks contradict the new selector | Accept legacy key, normalize to canonical assignment, explicit modern value wins. |
| HTMX loses focus or selected state | Stable control IDs, server-rendered selected values, bounded swap, ordinary GET fallback. |
| New list filters mark tickets read | Keep `upsert_ticket_view` exclusively in detail routes and assert it in tests. |
| Requester loses list context after live refresh or mutation | Sanitize and propagate the return URL through detail context, forms, redirects, and live-detail URL. |
| Search becomes slow at scale | Reuse current bounded reference/title query; measure production plans before proposing an index. |
| Compact rows become unreadable on mobile | One semantic DOM, explicit mobile labels, defined reflow checks, no horizontal page scroll. |

## 14. Explicit non-goals

- Database schema or migration changes.
- Persisted saved views, default-sort preferences, or per-user UI settings.
- Pagination or infinite scrolling without measured result-size/performance evidence.
- Bulk selection, bulk mutation, pick lists, or automatic “next ticket.”
- Client-side filtering or sorting.
- Custom priority/SLA/attention scores.
- Manual Board ranking or changes to status-column order.
- New ticket mutations, workflow rules, AI behavior, or `TicketView` semantics.
- A frontend framework, table/grid dependency, WebSocket, or new polling controller.
- A universal requester/Ops row template.

## 15. Acceptance criteria

The work is complete when:

1. Both list pages visually match the compact hierarchy of the current Board/detail work.
2. Important controls are visible without opening a disclosure.
3. Requester and Ops filters/orders are canonical, bookmarkable GET queries.
4. Defaults reproduce today's visible ticket set and recently-updated-first order.
5. All named sorts are deterministic and server-side.
6. Ops has one visible unassigned control and old URLs remain safe.
7. Board/List/detail/back/locale/HTMX navigation retains normalized state.
8. Requester output remains strictly requester-safe.
9. List visits still do not mark tickets viewed.
10. Full-page and no-JavaScript behavior remain complete.
11. 320px reflow, keyboard use, focus retention, result announcements, and EN/pt-BR are verified.
12. No migration or unrelated business-rule change is introduced.
13. The full automated suite and repository diff checks pass.

## 16. Brainstorm synthesis and decisions

Scores use novelty, viability, and fit on a 0–10 scale: `[N V F]`. These ideas were generated under logistics, game-design, on-call, inversion, and assumption-removal frames. The implementation plan above converges on the highest-fit ideas rather than implementing the entire pool.

### Cluster A — Stable scan geometry

- Compact “shipping label” with fixed scan zones `[N6 V9 F10]`
- Fixed metadata spine for state, age, owner, and activity `[N5 V9 F10]`
- Shared row grammar with predictable responsive anchors `[N5 V10 F10]`
- Quiet per-ticket changed-since-visit signals `[N6 V8 F9]`
- Stable age bands instead of undifferentiated dates `[N6 V7 F7]`
- Visual ticket radar plotting age and urgency `[N10 V4 F4]`

Decision: adopt compact aligned rows and existing changed markers; retain exact timestamps. Reject the radar and avoid introducing an implied SLA through age colors.

### Cluster B — Task and exception focus

- Dispatch waves such as now/next/later `[N8 V5 F6]`
- Exception lane for stale, failed-handoff, unowned work `[N7 V6 F7]`
- Needs-attention rail `[N6 V7 F8]`
- Deterministic attention-first ordering `[N7 V6 F8]`
- Exception digest links such as “urgent and unassigned” `[N8 V7 F7]`
- Deterministic next-ticket entrances `[N8 V5 F6]`

Decision: expose current authoritative attention flags and useful named sorts. Do not invent an aggregate attention policy, SLA, or next-ticket workflow in this phase.

### Cluster C — Reproducible state and recovery

- URL-resumable filter/sort run `[N6 V10 F10]`
- URL-backed named views `[N6 V8 F8]`
- Reversible sort-history breadcrumb `[N7 V5 F5]`
- Retain last successful rows during HTMX failure `[N7 V6 F7]`
- Return capsule preserving filters/order/view `[N7 V9 F10]`
- One canonical assignment grammar `[N6 V10 F10]`
- Diagnostic filtered-empty state `[N5 V10 F9]`
- Whitelisted deterministic server sorts `[N5 V10 F10]`

Decision: adopt canonical URLs, safe return context, canonical assignment, clear empty states, and named sorts. Ordinary full navigation is the failure fallback; no separate client cache is needed.

### Cluster D — High-speed operator interaction

- Keyboard speedrun layer `[N7 V5 F6]`
- Temporary local pick list `[N8 V4 F4]`
- Handoff manifest drawer `[N8 V4 F5]`
- Last-mile sequence based on current ordering `[N8 V5 F6]`
- Completion ghost after a row leaves the queue `[N7 V4 F5]`
- One-key next meaningful ticket `[N7 V5 F6]`

Decision: preserve excellent native keyboard/focus behavior and stable return state. Defer bespoke shortcuts, batches, drawers, and completion ghosts until real usage shows they are needed.

### Cluster E — Alternate navigation surfaces

- Keyboard ticket carousel `[N9 V3 F4]`
- Natural-language filter command palette `[N8 V4 F5]`
- Chronological scrubber `[N9 V4 F4]`
- Status/age/owner radar `[N10 V4 F4]`

Decision: reject for this scope. These replace an understandable list with novel interaction cost and weaker progressive enhancement.

### Converged shortlist

1. **Canonical visible filters and URL continuity** `[N6 V10 F10]` — highest operational value and fits the existing HTMX/server architecture.
2. **Compact stable row grammar** `[N5 V10 F10]` — directly fixes scan cost without changing data or permissions.
3. **Deterministic named server sorts** `[N5 V10 F10]` — answers the explicit ordering need safely and makes URLs reproducible.
4. **Filtered-empty diagnostics** `[N5 V10 F9]` — small, defensive, and prevents “missing data” confusion.

★ The non-obvious but viable pick is the canonical assignment grammar: removing one control improves capability because it eliminates contradictory states while preserving legacy URLs.

### Traps explicitly rejected

- **Attention-first composite sort:** no authoritative SLA/priority definition; would mix database and post-query projections.
- **Persisted saved views:** bookmarks already solve the current need without schema or preference lifecycle.
- **Client-side sort/filter:** breaks server authority, shareable URLs, and consistency with partial refreshes.
- **Universal requester/Ops row partial:** small reuse benefit with disproportionate privacy risk.
- **Automatic submit on every change:** increases request churn and makes multi-filter setup frustrating.
- **Pagination now:** no measured scale problem; adding it would multiply state/return/focus complexity.
- **Bulk/next-ticket workflows:** require product semantics beyond a visual/read-model redesign.

## 17. Follow-up threshold

After deployment, measure result counts and query duration before considering pagination or indexes. A later change is justified only if production evidence shows that the bounded reference/title search or all-row rendering is materially slow. That follow-up must preserve the same canonical query and deterministic ordering contract.

Provocation for a later product decision: if telemetry or operator feedback shows that the same canonical queue URLs are repeatedly shared, should a small set of team-curated shortcut links be promoted into navigation without introducing per-user saved-view persistence?
