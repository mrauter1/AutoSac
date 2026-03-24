# Test Strategy

- Task ID: implement-the-stage-1-ai-triage-mvp-hardening-an-c0aff52a
- Pair: test
- Phase ID: docs-and-release-validation
- Phase Directory Key: docs-and-release-validation
- Phase Title: Docs And Release Validation
- Scope: phase-local producer artifact

## Behavior-to-test map

- Auth redirect and safe-next handling
  - Covered by `tests/test_auth_requester.py`
  - Browser HTML GET redirects to `/login` with sanitized `next`
  - HTMX unauthenticated requests stay `401` and do not redirect
  - Wrong-role requests stay `403`
  - Safe-next rejects external, empty, and recursive `/login` targets

- Module-relative template and static paths
  - Covered by `tests/test_auth_requester.py`
  - Rendering and `/static/app.css` still work after `chdir()` away from repo root

- AI triage validation matrix
  - Covered by `tests/test_ai_worker.py`
  - Clarification path requires questions
  - Clarification path rejects `auto_public_reply_allowed=true`
  - `route_dev_ti` rejects any public reply and allows the no-public-reply path
  - Unknown tickets reject automatic public confirmation

- Prompt transport and run artifacts
  - Covered by `tests/test_ai_worker.py`
  - Prompt stays in `prompt.txt`
  - Full prompt is not present in Codex argv
  - Prompt is transported via stdin during execution

- HTMX fragment behavior and preserved view semantics
  - Covered by `tests/test_ops_workflow.py`
  - `/ops` and `/ops/board` return full HTML normally and fragments for HTMX
  - Queue, board, and filter refreshes do not mark tickets viewed
  - Ticket detail views still mark tickets viewed

- Bootstrap and worker hardening
  - Covered by `tests/test_foundation_persistence.py`, `tests/test_ai_worker.py`, and `tests/test_hardening_validation.py`
  - Admin bootstrap remains deterministic and idempotent
  - Worker heartbeat seeds missing `system_state.bootstrap_version`
  - Bootstrap, web smoke checks, and worker smoke checks succeed end to end

- Docs and env contract
  - Covered by `tests/test_hardening_validation.py`
  - `.env.example` includes all `Settings` environment variables
  - `README.md` documents install path, migration step, bootstrap order, admin/user CLI flow, smoke checks, and `bootstrap_version`

## Preserved invariants checked

- No redirect regression for HTMX auth failures
- Wrong-role access remains forbidden instead of redirecting across roles
- `/ops` and `/ops/board` filter refreshes preserve existing `ticket_views` semantics
- Prompt artifact and `final.json` contract remain intact while argv stays prompt-free

## Edge cases and failure paths

- Invalid or missing preauth login state and invalid CSRF remain covered in `tests/test_auth_requester.py`
- Clarification and routing contradiction cases remain covered in `tests/test_ai_worker.py`
- Script checks before workspace bootstrap remain covered in `tests/test_hardening_validation.py`

## Flake-risk management

- Web and route tests use patched dependencies and in-process `TestClient`
- Worker and bootstrap tests use temp directories and patched subprocess/session boundaries
- Script-level checks use temporary sqlite databases and isolated env vars

## Known gaps

- The docs contract test is string-based; it guards required operator-facing commands and variables but does not parse markdown structure.
