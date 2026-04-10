# Implement ↔ Code Reviewer Feedback

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: async-delivery-runtime
- Phase Directory Key: async-delivery-runtime
- Phase Title: Async Delivery Runtime
- Scope: phase-local authoritative verifier artifact

## Findings

- IMP-001 | blocking | `worker.slack_delivery.send_slack_webhook`
  `httpx.Timeout(timeout_seconds)` is a per-operation timeout, not a hard end-to-end deadline for the whole webhook attempt. A slow endpoint that keeps the socket alive with periodic reads can run longer than `SLACK_HTTP_TIMEOUT_SECONDS` and even longer than `SLACK_DELIVERY_STALE_LOCK_SECONDS`, which lets another worker recover the row as stale while the original POST is still in flight. That violates the PRD's hard-timeout contract and materially increases duplicate-send / attempt-state drift risk. Minimal fix: wrap the outbound POST in a real total-attempt deadline, treat expiry as the retryable timeout case from Section 9.4, and add a test that the request cannot outlive the configured total timeout window.

- IMP-002 | non-blocking | `worker.slack_delivery._sanitize_operator_summary`
  The helper currently collapses whitespace but does not actually redact secrets or URLs, even though the delivery code uses it as the central safety seam before persisting `last_error`. Today the current exception strings are probably benign, but this leaves secret hygiene dependent on upstream library messages and future sender changes. Minimal fix: centralize actual redaction in this helper or a sibling sanitizer so absolute URLs and Slack-hook-shaped tokens are stripped before storage/logging.

## Review Cycle 2

- `IMP-001` resolved in producer cycle 2: `send_slack_webhook` now wraps the full async webhook attempt in `asyncio.wait_for(...)`, so the delivery runtime enforces a real total deadline instead of relying only on `httpx` per-operation timeouts.
- `IMP-002` resolved in producer cycle 2: `_sanitize_operator_summary` now redacts absolute URLs and Slack-hook-shaped fragments before persisting/logging operator-facing errors.
- No new findings in this review cycle.
