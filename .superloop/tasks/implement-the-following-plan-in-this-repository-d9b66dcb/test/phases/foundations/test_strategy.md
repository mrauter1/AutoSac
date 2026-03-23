# Test Strategy

- Task ID: implement-the-following-plan-in-this-repository-d9b66dcb
- Pair: test
- Phase ID: foundations
- Phase Directory Key: foundations
- Phase Title: Foundations Hardening
- Scope: phase-local producer artifact

## Coverage map
- AC-1 cwd-independent app startup:
  - `test_create_app_and_template_paths_are_module_relative`
  - `test_login_template_renders_outside_repo_root`
  - `test_run_web_check_works_outside_repo_root_after_bootstrap`
- AC-2 malformed hash handling:
  - `test_verify_password_returns_false_for_malformed_hash`
  - `test_login_route_returns_invalid_credentials_for_malformed_hash`
- AC-3 Codex prompt transport:
  - `test_build_codex_command_matches_required_contract`
  - `test_execute_codex_run_uses_stdin_instead_of_prompt_argv`

## Preserved invariants checked
- `/static` mount remains present while path resolution becomes module-relative.
- Login failures on malformed hashes remain user-facing invalid-credential responses, not server errors.
- Codex still uses required flags, schema output, final output, image args, worker cwd, and env setup while removing the prompt from argv.

## Edge cases and failure paths
- Wrong password against a valid hash still returns `False`.
- Invalid/malformed argon2 hash returns `False`.
- Outside-repo execution is exercised both through direct app reloads and `scripts/run_web.py --check`.
- Codex execution test asserts stdin prompt delivery and verifies `final.json` loading still succeeds.

## Stabilization notes
- Outside-cwd template coverage reloads app modules under `tmp_path` and overrides auth dependencies so the test stays deterministic and isolated from database/session setup.
- Phase validation excludes the known README/.env contract test because that behavior belongs to the later docs/env phase, not foundations.

## Known gaps
- Docs/env acceptance coverage remains intentionally deferred to the later phase.
