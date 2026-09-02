# AutoSac UI/UX redesign plan

**Status:** implementation-ready plan; no redesign code is included

**Repository baseline:** `ui-overhaul` branch, inspected 2026-09-02

**Research access date:** 2026-09-02
**Primary objective:** make the Board operate like a focused Trello-style workflow and make requester/Ops ticket pages feel like conversation-first ChatGPT-style workspaces, without changing AutoSac's product semantics.

## 1. Executive summary

AutoSac does not need a frontend rewrite. Its server-rendered FastAPI/Jinja/HTMX architecture already has the hard parts needed for a responsive product: authoritative server projections, permission-specific fragments, ETag polling, incremental updates, preserved scroll position, and composer-safe refresh boundaries. The redesign should expose those capabilities through a clearer interaction model.

The recommended direction is:

- Turn the Board into a full-width, horizontally navigable workspace with compact, information-dense cards, persistent filters, and safe cross-status movement. Every drag operation must also be available through an ordinary **Move** command that works with keyboard, touch, and JavaScript disabled.
- Turn ticket pages into centered conversations with a natural, sticky composer and an in-thread AI-working placeholder. Keep operator metadata and analysis in a secondary inspector rather than interleaving them with the conversation.
- Make audience boundaries visible: public, internal, AI, and system entries must differ through words, icons/shapes, position, and color—not color alone. Requesters must continue to receive a server-side public-only projection.
- Preserve the existing live-update controller and fragment contracts. It must continue to update the header, ledger, analysis, and pending draft without replacing the composer, stealing focus, or moving a reader who is reviewing older content.
- Introduce a small token layer and page-specific CSS/JavaScript, but no SPA, component framework, design-system dependency, WebSocket infrastructure, or database migration.

The plan deliberately rejects several superficially attractive ideas: within-column ordering without a rank model, generic undo for workflow changes with side effects, chat branches, hidden chain-of-thought display, custom spatial keyboard controls, and a universal draft drawer. These add semantic or operational complexity without solving the core experience.

## 2. Evidence, method, and confidence

Three evidence classes are used:

| Mark | Meaning |
|---|---|
| **Observed** | Verified in current source or tests. |
| **Documented** | Supported by cited first-party product guidance or an authoritative standard. |
| **Recommended** | A project-specific design inference based on observed constraints and documented guidance. |

The current templates, routes, styles, JavaScript, localization, models, domain services, architecture notes, and tests were inspected. The rendered application was not exercised against real ticket data because no isolated seeded environment was available and production data was explicitly out of scope. Visual findings are therefore grounded in source structure, CSS, and tests rather than screenshots. Before implementation sign-off, phase 0 should capture local screenshots at the target viewports using synthetic data.

The most relevant repository sources are:

- `app/templates/base.html`, `ops_board.html`, `ops_board_columns.html`, and `ops_filters.html`
- `app/templates/requester_ticket_detail.html`, `requester_ticket_ledger.html`, `ops_ticket_detail.html`, `ops_ticket_ledger.html`, `ops_ticket_analysis.html`, and `ops_ticket_persistent_history.html`
- `app/static/app.css` and `app/static/ticket-live.js`
- `app/routes_ops.py`, `app/routes_requester.py`, `app/ticket_live.py`, `app/timeline.py`, and `app/ui.py`
- `shared/models.py` and `shared/ticketing.py`
- ticket-live, route, authorization, visibility, and template tests under `tests/`

## 3. Product principles and invariants

These are implementation gates, not aspirations:

1. **Conversation is canonical.** Every accepted ticket event remains in chronological history. No fork, branch, or alternate conversation is introduced.
2. **Visibility is enforced on the server.** Requester pages and live fragments contain public content only. Hiding internal content with CSS or JavaScript is never an access-control mechanism.
3. **Workflow meaning is preserved.** Status changes retain their existing business effects, including AI rerun behavior when entering AI Triage and existing Slack/event behavior.
4. **Direct manipulation has an equivalent command.** Dragging is convenience, never the only way to move a ticket.
5. **The user's unfinished writing is protected.** Polling, fragment replacement, navigation aids, and error paths must not clear or replace a composer.
6. **The server remains authoritative.** Pending feedback can be immediate, but canonical status and content always come back from a server response/fragment.
7. **Progress describes observable phases, not hidden reasoning.** Never fabricate percentages or expose private reasoning traces.
8. **Information density is earned by hierarchy.** Reduce panel chrome and repeated labels before removing useful operational information.
9. **Progressive enhancement is the default.** Core reading, replying, filtering, and moving work through links/forms without JavaScript.
10. **One visual language, explicit role variants.** Requester and Ops experiences share tokens and interaction rules, while maintaining separate permission-safe projections and templates.

## 4. Current architecture and user workflows

### 4.1 Runtime and presentation

**Observed:** AutoSac is a modular monolith with FastAPI routes, Jinja templates, a vendored HTMX client, one global stylesheet, and targeted vanilla JavaScript. PostgreSQL stores tickets, messages, views, AI runs, and drafts. The architecture is well suited to a server-driven redesign.

**Observed roles:**

- **Requester:** sees own tickets and public conversation, replies with attachments, and can resolve.
- **Dev/TI:** sees all tickets, assignment/status controls, public replies, internal notes, AI rerun, drafts, and analysis.
- **Admin:** has Ops capabilities plus user/Slack administration.

**Observed statuses:** New, AI Triage, Waiting on User, Waiting on Dev/TI, and Resolved.

**Observed event model:** messages include author type, visibility, source, body, and attachments. Status history is merged chronologically with messages. Requester serialization filters to public messages; Ops serialization includes internal content.

### 4.2 Live ticket behavior

**Observed:** `app/static/ticket-live.js` polls quickly while active and more slowly while idle, pauses in hidden tabs, uses ETags, backs off on failure, and requests versioned fragments. The server projects detailed Ops phases and coarser requester phases. Only the ticket header, ledger, AI analysis, and pending draft are replaced; the composer is intentionally outside the replacement boundary. Disclosure state/read position are preserved, and readers away from the bottom receive a new-update control instead of forced scrolling.

This is a strong foundation. The redesign should restyle and relocate its output, not replace the transport.

### 4.3 Important domain boundaries

- Ticket list/board views do not mark a ticket read; detail views update `TicketViews.last_viewed_at`.
- Board order is globally based on `Ticket.updated_at DESC`; there is no per-column rank.
- Moving a ticket to AI Triage can request a manual AI rerun. A visual status move is therefore not always reversible or side-effect free.
- Operator messages created outside AI Triage preserve current behavior: Waiting on User, Waiting on Dev/TI, and Resolved do not start Codex, but their content remains available to a later authorized run.
- Persistent Codex turn history is Ops-only and appears below **More Analysis**, closed by default. It is diagnostic history, not ticket-ledger content.

## 5. Current-state UX diagnosis

| Area | Current evidence | User cost | Priority |
|---|---|---|---|
| Board geometry | Five fixed columns sit inside an approximately 1180px content container; narrow screens stack columns vertically. | Cramped desktop scanning and loss of board spatial model on mobile. | P0 |
| Board cards | Cards omit assignee and useful update-time context; the entire card is mainly a detail link. | Operators open tickets just to answer basic triage questions. | P0 |
| Ticket movement | No drag/drop or visible Move action on cards. | Routine status management has unnecessary navigation and form cost. | P0 |
| Filters | A large form panel consumes vertical space; Board/List links do not consistently preserve current query context. | Board loses useful viewport and operators repeat filter work. | P0 |
| Requester ticket | Two-column transcript/composer presentation reads as a form page, not a conversation. | Replying feels detached from the thread. | P0 |
| Ops ticket | Conversation, AI, status, assignment, public reply, and internal note are distributed across many equally weighted panels. | Primary work is visually ambiguous and cognitively expensive. | P0 |
| Ledger hierarchy | Messages and status events use similarly heavy cards. | A long ticket becomes a wall of boxes; audience and authorship take effort to parse. | P0 |
| AI activity | Progress is displayed above the main layout rather than as the next developing conversational event. | Waiting state feels disconnected from the response destination. | P1 |
| Ops validation | Some invalid public/internal submissions produce generic HTTP 400 responses instead of returning the form with its text and specific error. | A mistake can destroy flow and may appear to lose writing. | P0 |
| Visual system | Warm gradients, large radii, and shadows are used broadly; page-specific CSS is concentrated in one large file. | Operational density suffers and future styling becomes harder to reason about. | P1 |
| Navigation | Sticky horizontal header wraps on mobile and has no strong current-location affordance. | Page hierarchy is harder to scan, especially on narrow screens. | P1 |
| Test coverage | Strong server/fragment tests; little browser-level keyboard, focus, reflow, or visual regression coverage. | Interaction regressions could pass the existing suite. | P1 |

## 6. Research synthesis: adopt, adapt, reject

### 6.1 Trello patterns

**Documented:** Trello supports direct card movement and a Move action, treats filtering as a way to retain board context, provides keyboard navigation/shortcuts, separates common actions from less-common actions, and uses named labels rather than color alone. Atlassian's drag-and-drop guidance requires visible affordances, meaningful accessible names, focus restoration, live announcements, and a non-drag alternative. ([Moving cards or lists](https://support.atlassian.com/trello/docs/moving-cards-or-lists/), [Filtering vs. searching](https://support.atlassian.com/trello/docs/filtering-vs-searching/), [Keyboard shortcuts](https://support.atlassian.com/trello/docs/keyboard-shortcuts-in-trello/), [New card back](https://support.atlassian.com/trello/docs/new-card-back/), [Adding labels](https://support.atlassian.com/trello/docs/adding-labels-to-cards/), [Atlassian drag-and-drop design](https://atlassian.design/components/pragmatic-drag-and-drop/design-guidelines), [drag-and-drop accessibility](https://atlassian.design/components/pragmatic-drag-and-drop/accessibility-guidelines/); accessed 2026-09-02.)

**Adopt:** spatial columns, dense cards, direct cross-column movement, visible Move alternative, filter-in-place, clear focus/feedback, optional keyboard shortcuts discoverable in the UI.

**Adapt:** AutoSac columns are consequential workflow states, not arbitrary lists. Movement must explain side effects and must not imply freely reversible ordering.

**Reject:** persistent manual ordering and WIP limits at this stage. AutoSac has neither a rank field nor an agreed capacity policy. Trello's table/list alternative is useful, but it should reuse AutoSac's existing List view rather than create a third representation. ([Single-board table view](https://support.atlassian.com/trello/docs/single-board-table-view/); accessed 2026-09-02.)

### 6.2 ChatGPT patterns

**Documented:** current ChatGPT guidance describes conversation continuity and sidebar organization; product release notes describe a simplified composer and visible planning/progress for longer work. ([Projects in ChatGPT](https://help.openai.com/en/articles/10169521-projects-in-chatgpt), [ChatGPT release notes](https://help.openai.com/en/articles/6825453-chatgpt-release-notes), [ChatGPT home page](https://help.openai.com/en/articles/9125172-the-chatgpt-home-page); accessed 2026-09-02.)

**Adopt:** conversation-first reading width, strong sender hierarchy, composer adjacent to the thread, restrained controls, visible ongoing work, and a secondary navigation/inspector layer.

**Adapt:** ticket messages have publication, audience, status, assignment, and approval semantics absent from an ordinary consumer chat. Public/internal mode and workflow consequences must remain explicit.

**Reject:** conversation branching and any presentation of hidden chain-of-thought. AutoSac's ticket ledger is a retained, auditable history; branches would obscure which events govern the ticket.

### 6.3 Accessibility and general usability

WCAG 2.2 requires an alternative to dragging, focus that is not obscured, status messages exposed to assistive technology, minimum target sizing, adequate contrast, and reflow except for genuinely two-dimensional content. The WAI disclosure pattern supplies the expected semantics for collapsed controls. ([WCAG 2.2](https://www.w3.org/TR/WCAG22/), [Understanding Dragging Movements](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements), [Understanding Reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow), [WAI Disclosure Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/); accessed 2026-09-02.)

NN/g's usability heuristics support visibility of system status, recognition over recall, error prevention, consistency, and user control. Progressive disclosure is appropriate for secondary analysis and advanced composer controls. GOV.UK guidance supports specific, field-adjacent errors that preserve entered values. ([Ten Usability Heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/), [Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/), [Designing for waits](https://www.nngroup.com/articles/designing-for-waits-and-interruptions/), [GOV.UK error message](https://design-system.service.gov.uk/components/error-message/); accessed 2026-09-02.)

## 7. Target information architecture and app shell

Use a stable shell with three conceptual layers:

```text
┌ slim application navigation ┐ ┌──────────────── work surface ────────────────┐
│ AutoSac                     │ │ page identity / context / primary actions    │
│ My tickets or Ops           │ │                                               │
│ Board                       │ │ board OR centered conversation + inspector   │
│ Admin (authorized only)     │ │                                               │
│ locale / account            │ │                                               │
└─────────────────────────────┘ └───────────────────────────────────────────────┘
```

- Desktop: a slim left rail (about 208–224px) and a fluid work surface. Board pages use the full work-surface width; ticket conversation width remains constrained.
- Tablet: compact rail or top-level menu, conversation plus collapsible inspector.
- Mobile: one page header with an accessible navigation disclosure; one content column. Do not make the whole page horizontally scroll. The Board alone may use horizontal column scrolling because it is intrinsically two-dimensional; the List view remains the reflow-safe alternative.
- Mark the current destination with visible styling and `aria-current="page"`.
- Preserve URL query parameters when switching Board/List or returning from a ticket. The URL remains the shareable source of filter truth.

Do not build a client-side router. Ordinary links, GET filters, HTMX fragments, and server-rendered pages are sufficient.

## 8. Board redesign

### 8.1 Layout and columns

The Board should be a full-bleed work canvas below a compact page header and toolbar. Each status is a stable column with:

- localized status name and concise workflow description;
- visible filtered count;
- a quiet status accent that is not the only identifier;
- empty-state guidance in the column body;
- a minimum useful width of roughly 280–304px.

Desktop columns scroll horizontally as a single board surface when needed; they should not be squeezed into the generic content max-width. At small widths, retain horizontal columns with scroll snapping and an explicit status jump control. Provide the existing List view as the one-dimensional accessible/mobile alternative, and preserve filter context between representations.

Do not allow lists themselves to be reordered. Status order is product workflow, not personal preference.

### 8.2 Card hierarchy

Recommended card anatomy:

```text
┌ T-000059                       ● Urgent ┐
│ Printer authentication repeatedly fails │
│ Requester name                              │
│ [Route: TI] [Draft awaiting review]        │
│ Assignee avatar/name             12 min    │
│ [Move ▾]                          [Open →]  │
└────────────────────────────────────────────┘
```

Rules:

- Row 1: reference, unread marker, urgent state. Never encode urgency by color alone.
- Title: two-line maximum with full title available to assistive technology/title tooltip.
- Context: requester, route, pending-draft/approval state.
- Footer: assignee (or explicit Unassigned) and localized last-update time; exact time in a `<time datetime>` tooltip or accessible label.
- The open-ticket link and Move control are separate targets; do not turn interactive controls into descendants of one giant anchor.
- Avoid message previews. They add query/data exposure complexity and unstable card heights while providing little triage value.
- Use initials only as a visual supplement; keep the assignee's accessible full name.

### 8.3 Moving tickets

Implement movement in two layers:

1. **Baseline command:** each card has a localized Move disclosure/menu containing every permitted destination except its current status. It submits a normal CSRF-protected form to the existing status route. This is the keyboard, touch, assistive-technology, and no-JavaScript path.
2. **Desktop enhancement:** small vanilla JavaScript enables cross-column drag/drop using the same destination command and endpoint. Drag handles/affordance appear on hover and focus; eligible columns are highlighted. No within-column reorder is offered.

Feedback state machine:

```text
idle → submitting (card remains in source, controls disabled, busy label)
     → success (apply authoritative board fragment, announce destination, restore focus)
     → failure (restore idle state, announce specific error and retry action)
```

This intentionally uses **receipted/pessimistic movement**: the card looks active immediately but does not permanently jump columns until the server accepts the mutation. That prevents flicker and false certainty when status changes have AI or notification side effects.

- Confirm only destinations with meaningful consequences: at minimum AI Triage (starts/requeues AI work) and Resolved. The confirmation names the effect.
- Do not add generic Undo. A reverse visual move cannot reliably undo an AI request, notification, or resolution side effect.
- Block duplicate submissions locally while one request is pending. If real multi-operator collisions emerge, add an `expected_status` service guard later; do not preemptively add database versioning.
- On success, refresh the canonical board projection instead of mutating counts/cards by hand.

Minimal route presentation enhancement is justified: accept a strict, allow-listed `return_to` whose parsed path is exactly the Ops Board or List path and whose query contains only supported filter keys. Normal forms receive the existing redirect pattern back to that safe URL. When `Accept: application/json` is explicitly sent by the Board enhancement, return `200` JSON containing the ticket reference and authoritative resulting status; the client then fetches the canonical Board fragment using the current filter URL. This avoids following a redirect through ticket detail (which could mark the ticket read). It changes neither status rules nor schema.

### 8.4 Filters, search, sorting, and context

Replace the large filter panel with:

- a compact search field for reference/title;
- primary filter button/disclosure;
- active-filter chips with individual removal;
- clear-all action;
- result count and Board/List toggle.

Keep all state in GET query parameters and preserve it in view toggles, ticket links, and allowed return links. Board status selection means visible/focused columns; List status selection continues to filter rows.

Retain current filters: route, assignee, urgent, unassigned, created by me, needs approval, and updated since viewed. A simple server-side `q` over normalized reference/title is a worthwhile read-model addition and requires no schema. Avoid saved filter presets until usage proves repeated configurations justify persistence. Avoid arbitrary client sorting on the Board because canonical `updated_at DESC` ordering is already meaningful; List may expose a small explicit sort set later.

### 8.5 Board states and edge cases

- **Empty column:** “No tickets in Waiting on User” plus no artificial call to action.
- **No filter results:** show active filters and a Clear filters action, not five ambiguous empty columns.
- **Loading/refresh:** retain current cards; use a subtle toolbar indicator rather than skeleton-replacing the board.
- **Stale:** after retry exhaustion, show “Updates paused” with Retry; never silently imply freshness.
- **Failure to move:** keep card at source, explain the server response, make Retry explicit.
- **Permission restricted:** omit controls server-side; do not render disabled actions that reveal unavailable capabilities.
- **Long titles/names/PT-BR:** clamp only the visible title, preserve full accessible text, and allow badges to wrap.

### 8.6 WIP, quick actions, and previews

- **WIP limits:** not recommended. Current columns express state, and no capacity policy/domain data exists.
- **Quick actions:** Move and Open only in the first release. Adding assignment/status menus simultaneously would make cards error-prone on touch.
- **Preview:** not recommended initially. The redesigned card contains the highest-value triage fields; detail remains one activation away.

## 9. Shared ticket conversation model

### 9.1 Conversation geometry

Use one document scroll, not nested scrolling regions.

- Center conversation content at approximately 760–820px for readable line length.
- Use open vertical spacing instead of wrapping every event in a large panel.
- Put the composer immediately after the conversation and make it sticky near the viewport bottom when space permits. Add `scroll-padding-bottom` so focused content is never hidden behind it.
- Keep the ticket title/reference, status, and primary navigation in a compact sticky header. Secondary metadata moves to an inspector/details region.

### 9.2 Event hierarchy

| Event | Visual treatment | Required labels |
|---|---|---|
| Requester message | requester view: compact right/tinted; Ops: public lane | author, Public where audience ambiguity exists, exact time |
| Human support public reply | left/neutral response block | author/team, Public, exact time |
| AI public response | left/neutral with AI glyph and explicit AI label | “AI response,” Public, exact time/source |
| Internal note | full-width or left block with amber audience rail/pattern | “Internal — Ops and Codex context,” author, exact time |
| System/status event | compact centered divider in chronology | old/new state and exact time |
| AI working projection | provisional final item, visually quieter/pulsing only when motion allowed | phase label; never stored as a message |

The Ops ledger may offer visual filters such as All / Public / Internal / System, but **All** is the default and filters must not alter underlying history. Requester markup must never contain internal entries.

Do not introduce causal branches or “reply-to” trees. Chronology remains the simple, auditable model.

### 9.3 New and unread events

Preserve the existing no-scroll-steal behavior. If the reader is close to the bottom, append/refresh naturally. Otherwise keep their position and show “New ticket update — Jump to latest.” After measuring the pre-view `TicketViews.last_viewed_at` before updating it, the server can optionally render a one-time “New since your last visit” divider without a schema change.

Deep links should target stable event IDs, focus/flash the target without excessive motion, and fall back to the ticket heading if the event is not visible to the current role.

## 10. Requester Ticket view

Recommended structure:

```text
Ticket header: Back to my tickets · T-000059 · status · Resolve
──────────────────────────────────────────────────────────────
                 public conversation
                 status dividers
                 provisional AI turn (when active)
                 new-update control
──────────────────────────────────────────────────────────────
                 sticky public reply composer
```

- Only public conversation appears; keep enforcement in `routes_requester.py`/server presenters.
- Move Resolve from the composer panel to the ticket header/actions area. Confirm it with a specific consequence statement.
- Composer includes textarea, attachment control/list, concise visibility statement (“Visible to you and the support team”), Send, and submission feedback.
- Preserve the entered body and selected-file display on validation failure wherever browser security permits. File inputs cannot be programmatically repopulated; explain that files must be reselected only when necessary.
- Coarse AI labels remain requester appropriate: queued, working, or taking longer. Do not expose specialist routing, internal phases, or draft review.
- On mobile, keep the composer in normal flow or safely sticky above the browser/keyboard; never obscure the latest message or focused controls.

## 11. Ops Ticket workspace

### 11.1 Desktop composition

```text
Back to Board · T-000059 · status · assignee · primary actions
┌──────────── centered conversation ────────────┐ ┌─ inspector ───────┐
│ public/internal/system chronological ledger   │ │ metadata          │
│ provisional AI turn / new update              │ │ AI analysis       │
│ unified-looking audience composer             │ │ pending draft     │
└───────────────────────────────────────────────┘ │ advanced controls │
                                                  └───────────────────┘
```

The inspector is sticky on wide screens, enters normal document flow below the conversation on narrow screens, and uses native disclosures for secondary sections. Do not start with a complex off-canvas drawer.

Priority in the inspector:

1. pending draft requiring action;
2. current assignment/status and concise metadata;
3. AI summary/route/current run;
4. **More Analysis** disclosure;
5. persistent Codex history as its own disclosure below More Analysis, closed by default;
6. less frequent actions.

Persistent Codex history stays out of the message ledger and remains Ops-only.

### 11.2 Public/internal composer

Present public reply and internal note as one visual composer shell with two explicit modes, while retaining two separate server forms/endpoints and independent textareas.

- **Public reply:** “Requester will see this.” Preserve the current next-status behavior. Show the default consequence as a chip/sentence; place alternative next status/routing under Advanced options.
- **Internal note:** “Only Ops sees this; it may be included in future Codex context.” Do not invent a special AI-guidance message type.
- Switching modes hides but does not clear the other mode's form. Each retains its draft in the DOM.
- The live controller continues to exclude the entire composer shell from swaps.
- Use field-adjacent errors and re-render the same page/form with the user's body intact. Replace generic Ops HTTP 400 validation pages; this is an essential correctness/UX fix.
- Disable only the submitted mode while pending, label it “Sending…,” and restore it with a specific error on failure.

Optional after security review: store each mode's text in `sessionStorage`, keyed by ticket/path/mode, clearing on successful submission. Prefer session storage over local storage because internal notes may be sensitive. This is not required for initial implementation; DOM preservation and validation-safe rerender are.

### 11.3 Metadata and actions

Assignment and status stay available without dominating the conversation. Use labeled compact controls in the inspector; preserve ordinary forms and CSRF. Consequential changes require specific confirmation, not a generic “Are you sure?”. Rerun AI explains that it adds/requeues work on the same retained Codex conversation.

## 12. AI activity, live updates, and resilience

Render an always-present but normally hidden provisional slot at the end of each ledger. It is a projection of the latest run, not a message:

- use a stable non-message DOM ID and the latest run ID;
- show coarse requester or detailed Ops phase names from the existing server projection;
- include elapsed/delayed state for Ops when useful;
- use `role="status"`/an appropriate polite live region for meaningful phase changes, suppressing repetitive elapsed-time announcements;
- respect `prefers-reduced-motion`; a text/status change is sufficient.

Reconciliation must be explicit:

```text
active run
  → terminal state observed
  → awaiting successful fragment for terminal content_version
  → canonical message, internal entry, or pending draft rendered
  → provisional slot hidden
```

Never hide the provisional turn merely because `active=false` arrived if the matching fragment failed. Discard out-of-order responses, and never show both a stale provisional turn and its canonical artifact. A failed run becomes a concise failed state with the role-appropriate next action; it must not look like a published message.

Retain all current live-controller guarantees:

- 3s active/15s idle cadence, hidden-tab pause, ETag usage, and backoff unless measured evidence supports adjustment;
- no full-page reload;
- no composer replacement;
- preservation of focus, disclosure state, and reading position;
- no requester disclosure of internal-only work;
- pending Ops draft is a valid canonical landing target;
- update notification rather than forced scroll when reading older content.

## 13. Visual language and design tokens

Keep AutoSac's green identity, but move from decorative beige panels to a neutral work canvas and clearer semantic surfaces.

Proposed token groups (exact colors require contrast testing with the rendered app):

```css
--canvas; --surface; --surface-subtle; --border;
--text; --text-muted; --accent; --accent-hover; --focus;
--danger; --warning; --success;
--audience-public; --audience-internal; --source-ai; --source-system;
--space-1: .25rem; --space-2: .5rem; --space-3: .75rem;
--space-4: 1rem; --space-6: 1.5rem; --space-8: 2rem;
--radius-sm; --radius-md; --shadow-raised;
```

Guidance:

- System font stack; do not add a network font dependency.
- 10–16px component radii; reserve fully rounded shapes for pills/avatars.
- Shadows only for raised/transient elements, not every section.
- Body text at least 16px; muted metadata remains contrast compliant.
- Prefer 44px interactive targets; never below WCAG's 24 CSS px minimum without an allowed exception.
- Use a small bundled SVG icon sprite. Icons supplement text and accessible names; they do not replace them for consequential actions.
- Motion: short opacity/transform feedback only; no layout-shifting flourish. Remove nonessential animation under reduced motion.
- No dark mode in this scope.

## 14. Responsive, keyboard, touch, and localization behavior

Test layout behavior at content-driven breakpoints rather than targeting devices:

- **Wide (about 1200px+):** left navigation, fluid Board; conversation plus inspector.
- **Medium (about 768–1199px):** compact navigation; inspector enters collapsible flow or narrower side region.
- **Narrow (below about 768px):** one-column tickets; Board horizontal workspace plus status jump; List alternative prominent.
- **Reflow audit:** 320 CSS px and 400% zoom. Only the Board canvas may require two-dimensional scrolling; card contents and ticket pages must reflow.

Keyboard/touch contract:

- visible focus; logical DOM/tab order; Escape closes menus; activation returns focus to the invoking control/card;
- Move menu is the primary accessible movement control;
- do not implement a bespoke arrow-key drag grammar in the first release—Atlassian itself cautions that such controls can conflict with screen-reader navigation and add complexity;
- touch does not depend on hover or precision drag;
- sticky composer/header never obscures focus.

Localization:

- add every label/error/status to existing EN and pt-BR catalogs in the same change as its UI;
- test 30–50% text expansion and long Portuguese labels;
- use locale-aware display while retaining machine-readable `<time datetime>` values;
- avoid concatenated sentence fragments and hard-coded status names in JavaScript.

## 15. Component and code organization

Recommended file boundaries, preserving the current stack:

```text
app/templates/
  base.html                         shell + page asset blocks
  ops_board.html                    board page/toolbar
  ops_board_columns.html            canonical replaceable board fragment
  partials/ops_board_column.html    explicit column
  partials/ops_board_card.html      card + ordinary Move form
  requester_ticket_detail.html      requester conversation composition
  requester_ticket_ledger.html      public-only timeline + provisional slot
  ops_ticket_detail.html            conversation + inspector composition
  ops_ticket_ledger.html            Ops audience timeline + provisional slot
  partials/ops_ticket_composer.html two explicit forms in one shell

app/static/
  app.css                           tokens, shell, shared primitives
  board.css                         Board-only layout/components
  ticket.css                        conversation/inspector/composer
  board.js                          progressive Move/drag enhancement
  ticket-live.js                    retained, small reconciliation extension
```

Jinja templates for requester and Ops ledgers remain separate. Share tokens and CSS roles, not one overly generic ledger macro that risks mixing visibility semantics. Keep presentation formatting in existing route serializers/presenter helpers rather than adding model methods or template-side business logic.

`base.html` should offer optional `page_styles` and `page_scripts` blocks so Board code is not loaded on every page. No bundler is required.

Likely route-level changes:

- preserve/search GET query state in Ops list/board;
- serialize assignee/update/card flags already available to authorized Ops;
- allow-list Board/List `return_to` and provide an explicit enhanced status-mutation response;
- rerender Ops composers with field errors/values;
- optionally capture prior view timestamp before the existing upsert for an unread divider.

No changes are planned for `shared/models.py`, database migrations, or core status/AI business rules.

## 16. Recommendation matrix and backend-change discipline

| Recommendation | Value | Cost/risk | Decision |
|---|---|---|---|
| Full-width Board and dense cards | High scanning benefit | Low; template/CSS | Do now |
| Query-preserving filter toolbar | High continuity benefit | Low | Do now |
| Reference/title search | High retrieval benefit | Low route/read-model work; possible unindexed scan at large scale | Do now; measure before adding index |
| Move menu + cross-column DnD | High workflow benefit | Medium; consequential mutations/focus/error handling | Do now in progressive layers |
| Per-column manual ordering | Familiar Trello behavior | High; rank schema, concurrency, semantics | Reject for this redesign |
| Expected-status concurrency guard | Prevents rare operator collisions | Medium service/API behavior | Defer until collisions are observed |
| WIP limits | Possible process visibility | High policy/business-rule cost | Reject without product policy |
| Conversation-first ticket pages | High comprehension/reply benefit | Medium template/CSS | Do now |
| Unified-looking Ops composer | High clarity | Medium; privacy/error paths | Do now, keep endpoints separate |
| Validation-safe Ops forms | High data-loss prevention | Low/medium route work | Do first |
| Provisional in-thread AI turn | High waiting-state clarity | Medium reconciliation risk | Do with versioned state machine/tests |
| Session draft recovery | Moderate resilience | Privacy/security concern | Optional after review |
| Unread divider using existing view time | Moderate orientation value | Low route/presentation work | Phase 4; no schema |
| Chat branching/causal tree | Conflicts with canonical ticket history | High schema/UI complexity | Reject |
| WebSockets/SSE | Marginal latency improvement over proven polling | Operational complexity | Reject until measurements show need |
| Frontend framework/design-system package | Little project-specific benefit | Rewrite/dependency cost | Reject |

The only essential non-template changes are presentation contracts around Board mutation responses, search, and error rerendering. None changes database shape or core business rules. If search becomes slow at measured production scale, a later index migration should document query plans, deploy concurrently where supported, and keep the existing query as rollback; it is not part of this redesign.

## 17. Phased implementation plan

Each phase should be independently reviewable and deployable behind the existing behavior where practical.

### Phase 0 — Characterization and visual baseline

1. Seed synthetic tickets covering roles, all statuses, public/internal/AI/system entries, attachments, pending drafts, failures, long content, and EN/pt-BR.
2. Capture current screenshots at 390, 768, 1024, and 1440px.
3. Add authorization/visibility characterization tests before template extraction.
4. Document existing fragment IDs, live-controller invariants, query behavior, and focus/scroll expectations.

Exit: a failing change to requester visibility, composer replacement, or status side effects is caught before redesign work begins.

### Phase 1 — Foundations and Board reading experience

1. Add semantic tokens and shell/page asset blocks; add `aria-current`.
2. Split Board/Ticket CSS by page without changing behavior first.
3. Make Board full-width; implement column headers, compact cards, responsive horizontal workspace, and prominent List alternative.
4. Convert filters to compact disclosure/chips; preserve query across all Board/List/ticket navigation.
5. Add simple reference/title search and clear empty/stale/error presentations.

Exit: Board is substantially more scannable without introducing mutation JavaScript.

### Phase 2 — Safe Board movement

1. Add ordinary per-card Move forms and tests first.
2. Add strict `return_to` and explicit enhanced-response contract.
3. Add vanilla cross-column drag/drop backed by the same command.
4. Add consequence-specific confirmations, pending state, canonical fragment refresh, error/retry, announcements, and focus restoration.
5. Test duplicate input, stale fragments, network failure, forbidden moves, touch, keyboard, and JavaScript-disabled behavior.

Exit: all permitted moves work without dragging; enhanced movement never fabricates server state or marks a ticket read accidentally.

### Phase 3 — Requester conversation

1. Restyle public ledger/event hierarchy and move composer beneath the conversation.
2. Integrate the coarse provisional AI turn with versioned reconciliation.
3. Place Resolve in ticket actions with specific confirmation.
4. Validate mobile keyboard/sticky behavior, attachments, long content, and update-without-scroll-steal.

Exit: requester can read/reply during live updates without exposure, data loss, or focus movement.

### Phase 4 — Ops workspace

1. Add Ops audience lanes/labels and compact system events.
2. Build conversation + inspector layout; retain More Analysis and closed persistent-history disclosure order.
3. Combine public/internal composers visually while retaining separate forms and independent drafts.
4. Fix field-specific errors and value retention before adding enhancement behavior.
5. Integrate detailed provisional AI turn and pending-draft reconciliation.
6. Add the optional unread divider; consider session draft recovery only after privacy review.

Exit: an operator can understand audience, respond, monitor AI work, and act on a draft without losing place in the conversation.

### Phase 5 — Hardening and measured polish

1. Run full accessibility, localization, reduced-motion, zoom/reflow, and browser matrix.
2. Add a small browser-level suite for four critical flows: Board move, requester live arrival while typing, Ops public/internal drafts, and off-screen new-update navigation.
3. Measure payload size, polling traffic, layout shift, and Interaction to Next Paint; optimize only measured bottlenecks. Web.dev recommends immediate visual feedback and an INP p75 target at or below 200ms. ([INP](https://web.dev/articles/inp), [Optimize INP](https://web.dev/articles/optimize-inp); accessed 2026-09-02.)
4. Compare post-change task results and screenshots to the phase 0 baseline.

Exit: release gates below pass and no framework/backend expansion was needed.

## 18. Acceptance criteria, regression tests, and metrics

### 18.1 Functional acceptance

- Board displays all authorized tickets in canonical status/order with assignee, urgency, route, draft state, requester, and update context.
- Board/List/search/filter state survives switching views, opening a ticket, and returning.
- Every allowed status move works with keyboard/touch/no JavaScript; DnD only adds convenience.
- A failed move leaves the ticket in its authoritative source status and offers a retry.
- AI Triage and Resolve consequences are explicitly confirmed; core domain effects remain unchanged.
- Requester HTML and live fragments never contain internal text, attachments, detailed internal AI phases, analysis, or persistent history.
- Public/internal/AI/system events are distinguishable without color.
- Typing survives every live refresh; the composer is never inside a replaced fragment.
- Ops invalid submissions return field-specific errors and the entered body.
- Provisional AI state remains until its canonical terminal fragment arrives; out-of-order/failed responses do not create gaps or duplicates.
- Readers away from the bottom are not scrolled; the new-update action reaches the correct visible event.
- Persistent Codex history remains Ops-only, below More Analysis, closed by default.
- All new UI text exists in EN and pt-BR.

### 18.2 Automated coverage

Retain the full existing test suite and add:

- route-level sentinel tests for public/internal authorization in full pages and live fragments;
- standard Move form, CSRF, allow-listed return path, enhanced response, forbidden destination, and unchanged side-effect tests;
- query preservation and search normalization tests;
- Ops validation/value-retention tests for both composer modes;
- terminal live reconciliation tests covering delayed, failed, auto-published, manual-only, and pending-draft outcomes;
- static/DOM contract tests that replacement fragments exclude composers;
- targeted browser tests for focus restoration, no-scroll-steal, disclosure preservation, draft survival, and no-JS movement.

Avoid broad pixel snapshots for every page. Keep a few stable viewport screenshots for high-level layout and use DOM/behavior tests for semantics.

### 18.3 Manual accessibility matrix

- keyboard-only and screen-reader smoke test in a current Chromium and Firefox;
- 200% and 400% zoom, 320 CSS px reflow;
- reduced motion, high contrast/forced colors where supported;
- touch target and mobile virtual-keyboard checks;
- contrast verification for every semantic token/state;
- long EN/PT-BR labels, long unbroken text, attachments, and empty/error states.

### 18.4 Outcome measures

Establish a synthetic baseline, then compare:

- median interactions/time to find an unassigned urgent ticket and move it;
- error rate for public versus internal reply selection;
- percent of invalid submissions retaining entered text;
- time from AI terminal state to visible canonical artifact;
- unintended scroll/focus movement during live updates (target zero);
- Board status-move failures/retries and duplicate requests;
- p75 INP (target ≤200ms where field measurement is available), layout shift, and polling request/payload volume no worse than baseline.

## 19. Rollout, rollback, risks, and non-goals

### 19.1 Rollout and rollback

- Ship by vertical phase, not as one visual flag day.
- Keep route/domain contracts backward compatible while the old templates/forms are still callable.
- For high-risk Board movement and live reconciliation, use a narrow feature flag if the project already has a lightweight flag mechanism; do not add a flag platform solely for this work.
- Deploy synthetic/staging verification before production. Monitor HTTP 4xx/5xx, move failures, live polling errors, and AI terminal-without-content states.
- Rollback is template/static/route-code rollback; no database rollback is required because the essential plan has no migration.
- If enhanced DnD fails, remove/disable `board.js`; ordinary Move forms continue to work.
- If provisional reconciliation fails, hide the visual projection and fall back to existing progress placement while retaining canonical polling.

### 19.2 Principal risks and mitigations

| Risk | Mitigation |
|---|---|
| Internal content leaks through shared rendering | Separate role presenters/templates; sentinel authorization tests on pages and fragments. |
| A move starts duplicate AI work | one in-flight request, controls disabled, canonical response, preserve service tests; add expected-status guard only if needed. |
| Live terminal state appears before content | explicit active/awaiting-fragment/reconciled state keyed by run/content version. |
| Composer hidden by mobile keyboard/sticky UI | one document scroll, scroll padding, viewport tests, normal-flow fallback. |
| Visual density harms comprehension | semantic hierarchy, progressive disclosure, task testing—not larger generic panels. |
| New CSS creates cascade regressions | token/core/page stylesheet boundaries and staged behavior-preserving extraction. |
| Search query degrades at scale | measure query plan/latency; only then consider an index migration. |
| Session draft storage exposes internal text | optional, session-only, security review, clear on submit/logout; omit initially. |

### 19.3 Explicit non-goals

- No SPA/framework rewrite, frontend build pipeline, external design-system package, or network fonts.
- No database migration, rank model, WIP policy, saved-view model, or message-reply tree.
- No change to ticket status meanings, AI authorization, publication, visibility, Slack side effects, or Codex session continuity.
- No full-page reload for live ticket updates.
- No hidden-reasoning/chain-of-thought UI, invented percent-complete, or ChatGPT-style branches.
- No generic Undo for consequential workflow mutations.
- No requester access to internal content, AI analysis, pending internal drafts, or persistent Codex turn history.
- No dark mode, broad admin redesign, global command palette, or speculative notification center in this scope.

### 19.4 Final implementation gate

The plan makes the two potentially ambiguous scope decisions now: AI Triage and Resolved require consequence-specific confirmation, and session-storage draft recovery is excluded from the initial implementation. Phase 0's synthetic rendered baseline is the first implementation task, not an outstanding discovery dependency. Any later expansion of confirmations or client-side draft storage requires separate evidence and review.

This scope is intentionally bounded: it achieves the Trello-like and ChatGPT-like qualities through clearer hierarchy, direct but safe interaction, and resilient server-driven updates while leaving AutoSac's trusted architecture and domain model intact.
