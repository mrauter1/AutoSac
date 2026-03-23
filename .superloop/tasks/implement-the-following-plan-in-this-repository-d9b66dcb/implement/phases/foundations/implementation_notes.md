# Implementation Notes

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: implement
- Phase ID: foundations
- Phase Directory Key: foundations
- Phase Title: Foundations Hardening
- Scope: phase-local producer artifact

## Files changed
- `app/main.py`
- `app/ui.py`
- `shared/security.py`
- `worker/codex_runner.py`
- `tests/test_hardening_validation.py`
- `tests/test_auth_requester.py`
- `tests/test_ai_worker.py`

## Symbols touched
- `app.main.APP_DIR`
- `app.main.STATIC_DIR`
- `app.main.create_app`
- `app.ui.APP_DIR`
- `app.ui.TEMPLATES_DIR`
- `app.ui.templates`
- `shared.security.verify_password`
- `worker.codex_runner.build_codex_command`
- `worker.codex_runner.execute_codex_run`

## Checklist mapping
- A1: module-relative static/template resolution implemented in `app/main.py` and `app/ui.py`; covered by cwd-sensitive app/template tests and outside-root `run_web.py --check` test.
- A2: malformed/invalid argon2 hash handling broadened in `shared/security.verify_password`; covered by direct verification tests and login route non-500 regression test.
- A3: Codex prompt transport switched from prompt argv tail to stdin mode with fixed `-` arg; `prompt.txt`, `final.json`, flags, env, and image args preserved; covered by command and execution tests.

## Assumptions
- Installed Codex CLI supports stdin prompt delivery when prompt arg is omitted or set to `-`; verified locally with `codex exec --help`.

## Preserved invariants
- `/static` mount path and template names unchanged.
- `verify_password` signature unchanged and still fails closed to `False`.
- Codex required flags, output schema file, final output file, image args, worker cwd, timeout, and env behavior unchanged.

## Intended behavior changes
- Web static/template lookup no longer depends on the current working directory.
- Malformed stored password hashes are treated as invalid credentials instead of surfacing verification errors.
- Codex prompt content is no longer exposed on the subprocess argv tail.

## Known non-changes
- No auth/browser redirect or login CSRF flow changes in this phase.
- No HTMX, bootstrap/system-state, README, or `.env.example` changes in this phase.

## Expected side effects
- Worker subprocess inspection now shows a fixed `-` prompt arg rather than the full prompt body.

## Validation performed
- `pytest -q /workspace/AutoSac/tests/test_hardening_validation.py -k 'not test_env_example_and_readme_capture_acceptance_contract' /workspace/AutoSac/tests/test_auth_requester.py /workspace/AutoSac/tests/test_ai_worker.py`
- Result: `36 passed, 1 deselected`
- Observed existing out-of-phase failure when including full `test_hardening_validation.py`: `test_env_example_and_readme_capture_acceptance_contract` fails because current `README.md` does not yet mention `.env.example`.

## Deduplication / centralization
- Path resolution kept local to existing module globals in `app.main` and `app.ui`; no new shared helper introduced because only those two modules depend on cwd-sensitive paths in this phase.
