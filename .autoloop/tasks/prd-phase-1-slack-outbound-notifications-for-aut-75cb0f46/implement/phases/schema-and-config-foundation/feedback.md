# Implement ↔ Code Reviewer Feedback

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: schema-and-config-foundation
- Phase Directory Key: schema-and-config-foundation
- Phase Title: Schema and Config Foundation
- Scope: phase-local authoritative verifier artifact

- `IMP-001` `blocking` [shared/config.py::_load_slack_settings] Missing or empty `SLACK_TARGETS_JSON` currently bypasses validation because the helper only parses non-empty values. With `SLACK_ENABLED=true`, that leaves `settings.slack.is_valid=True` for a config the PRD treats as globally invalid under Sections 8.2 and 8.3. That would let later emission/delivery code treat Slack as configured when the required target object is actually absent, producing the wrong suppression state and the wrong operator signal. Minimal fix: make `_load_slack_settings()` mark missing/blank `SLACK_TARGETS_JSON` as invalid whenever Slack is enabled, and add a direct test for unset/empty env cases alongside the existing malformed-JSON coverage.
- Re-review closure: `IMP-001` is resolved. `_load_slack_settings()` now marks missing or empty `SLACK_TARGETS_JSON` as invalid when Slack is enabled, and the new parametrized test covers both unset and empty env cases. I found no new blocking or non-blocking findings in this phase after the fix.
