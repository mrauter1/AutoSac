from __future__ import annotations

from pathlib import Path

SESSION_COOKIE_NAME = "triage_session"
PREAUTH_LOGIN_COOKIE_NAME = "triage_preauth_login"
CSRF_FORM_FIELD = "csrf_token"

APP_ROUTES = (
    "/login",
    "/logout",
    "/app",
    "/app/tickets",
    "/app/tickets/new",
    "/app/tickets/{reference}",
    "/app/tickets/{reference}/reply",
    "/app/tickets/{reference}/resolve",
    "/attachments/{attachment_id}",
    "/ops",
    "/ops/board",
    "/ops/tickets/{reference}",
    "/ops/tickets/{reference}/assign",
    "/ops/tickets/{reference}/set-status",
    "/ops/tickets/{reference}/reply-public",
    "/ops/tickets/{reference}/note-internal",
    "/ops/tickets/{reference}/rerun-ai",
    "/ops/drafts/{draft_id}/approve-publish",
    "/ops/drafts/{draft_id}/reject",
    "/healthz",
    "/readyz",
)

CLI_COMMAND_NAMES = (
    "create-admin",
    "create-user",
    "set-password",
    "deactivate-user",
)

DEFAULT_UPLOADS_DIR = Path("/opt/triage/data/uploads")
DEFAULT_TRIAGE_WORKSPACE_DIR = Path("/opt/triage/triage_workspace")
DEFAULT_REPO_MOUNT_DIR = DEFAULT_TRIAGE_WORKSPACE_DIR / "app"
DEFAULT_MANUALS_MOUNT_DIR = DEFAULT_TRIAGE_WORKSPACE_DIR / "manuals"
DEFAULT_RUNS_DIR = DEFAULT_TRIAGE_WORKSPACE_DIR / "runs"

WORKSPACE_AGENTS_RELATIVE_PATH = Path("AGENTS.md")

WORKSPACE_BOOTSTRAP_VERSION = "stage1-v5"

WORKSPACE_AGENTS_CONTENT = """This repository is the Stage 1 custom triage workspace.

You are operating inside the Stage 1 ticket-analysis environment.

Hard rules:
1. Stage 1 is read-only.
2. Do not modify files under app/ or manuals/.
3. Do not inspect live databases or operational logs. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when they help answer the ticket.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, attached images, files under manuals/, and files under app/.
6. Inspect manuals/ and app/ whenever repository or process evidence would materially improve correctness.
7. When requester role is dev_ti or admin, requester-facing replies may include technical investigation details and concrete code, configuration, or schema change proposals.
8. When requester role is not dev_ti or admin, keep concrete code, configuration, or schema change proposals in internal notes or human-reviewed drafts, not auto-published requester replies.
9. When the question is not document-scoped, you may answer using general reasoning.
10. Never promise a fix, implementation, release, or timeline.
11. Prefer concise requester-facing replies.
12. For document-scoped questions, search the relevant sources before answering.
13. If a document-scoped answer is low risk and useful but you cannot verify it in manuals/ or app/, clearly say it was not verified.
14. Return only the final JSON object that matches the provided schema.
15. Treat screenshots as evidence but do not claim certainty beyond what is visible.
16. Never execute edits, patches, commits, branches, migrations, or database changes in Stage 1.
17. Internal messages may inform internal analysis but must not be disclosed publicly unless the same information is already present in public ticket content.
18. Perform every Stage 1-safe probe you can before concluding. Do not tell the internal team to run a repo/manual/ticket-evidence probe that you could have done yourself.
19. Separate verified facts, strong hypotheses, and unknowns. Never present an unverified guess as confirmed.
20. When the exact fix or root cause cannot be confirmed, provide enough internal context and concrete next steps for the next operator to continue safely.
"""
