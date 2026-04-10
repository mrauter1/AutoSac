# Implement ↔ Code Reviewer Feedback

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: admin-ui-and-user-mapping
- Phase Directory Key: admin-ui-and-user-mapping
- Phase Title: Admin UI and User Slack Mapping
- Scope: phase-local authoritative verifier artifact

- IMP-001 | blocking | [shared/slack_dm.py](/home/marcelo/code/AutoSac/shared/slack_dm.py#L97), [app/i18n.py](/home/marcelo/code/AutoSac/app/i18n.py#L297), [app/routes_ops.py](/home/marcelo/code/AutoSac/app/routes_ops.py#L697): The new Slack settings form still has English-only validation paths for the delivery tuning fields. `validate_slack_dm_settings_input()` raises messages like `message_preview_max_chars must be greater than or equal to 4`, but those strings are not mapped in `translate_error_text()`. As a result, a `pt-BR` admin submitting invalid numeric settings gets an English error on the new admin Slack screen, which misses the phase requirement for locale-aware errors. Add i18n keys and translation-pattern coverage for every new Slack settings validation message, then cover at least one localized numeric-validation failure in the Slack integration route tests.
