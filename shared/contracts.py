from __future__ import annotations

from pathlib import Path

SESSION_COOKIE_NAME = "triage_session"
PREAUTH_LOGIN_COOKIE_NAME = "triage_preauth_login"
CSRF_FORM_FIELD = "csrf_token"
WORKSPACE_BOOTSTRAP_VERSION = "stage1-v1"

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
WORKSPACE_SKILL_RELATIVE_PATH = Path(".agents/skills/stage1-triage/SKILL.md")

WORKSPACE_AGENTS_CONTENT = """This repository is the Stage 1 custom triage workspace.

You are performing Stage 1 ticket triage only.

Hard rules:
1. Stage 1 is read-only.
2. Do not modify files under app/ or manuals/.
3. Do not inspect databases, DDL, schema dumps, or logs.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, attached images, files under manuals/, and files under app/.
6. Search manuals/ and inspect app/ when the request is about internal app behavior, app usage, or an internal process that may be documented there.
7. When the question is not document-scoped, you may answer using general reasoning.
8. Distinguish among: support, access_config, data_ops, bug, feature, unknown.
9. Ask at most 3 clarifying questions.
10. Never promise a fix, implementation, release, or timeline.
11. Prefer concise requester-facing replies.
12. For document-scoped questions, search the relevant sources before answering.
13. If a document-scoped answer is low risk and useful but you cannot verify it in manuals/ or app/, you may give a best-effort guess only if you clearly say it was not verified.
14. Return only the final JSON object that matches the provided schema.
15. Treat screenshots as evidence but do not claim certainty beyond what is visible.
16. Reserve human review for misuse, safety concerns, or uncertainty that would make a direct answer unsafe.
17. impact_level means business/user impact in Stage 1, not technical blast radius.
18. development_needed is a triage estimate only.
19. Never propose edits, patches, commits, branches, migrations, or database changes in Stage 1.
20. Internal messages may inform internal analysis and routing.
21. Do not disclose internal-only information in automatic public replies unless the same information is already present in public ticket content.
22. When human review is needed, still produce an actionable internal note and a short requester-facing draft that says the internal team is reviewing the request.
"""

WORKSPACE_SKILL_CONTENT = """---
name: stage1-triage
description: Classify a ticket, use manuals/ and app/ only when the question is document-scoped, answer low-risk general questions directly, and produce actionable human-review handoff notes when needed. Never modify code, never inspect databases, and never propose patches.
---

Use this skill when:
- the task is a support ticket, internal request, bug report, or feature request written in natural language
- screenshots may help clarify the request
- the workspace contains app/ and manuals/
- the output must be structured JSON for automation

Do not use this skill when:
- code modification is required
- patch generation is required
- database or DDL analysis is required
- external web research is required

Workflow:
1. Read the ticket title and all relevant ticket messages carefully.
2. Decide whether the request is document-scoped or can be answered with general reasoning.
3. Search manuals/ and inspect app/ only when the request is document-scoped.
4. Use attached images when relevant.
5. Classify the ticket into exactly one class.
6. Determine if the ticket likely needs development.
7. Determine if clarification is needed.
8. If clarification is needed, ask only the minimum high-value questions, maximum 3.
9. If the request is not document-scoped and risk is low, answer with general reasoning.
10. If a document-scoped answer is low risk but you could not verify it in manuals/ or app/, you may give a best-effort answer only if you clearly say it was not verified.
11. Use human review only for safety, misuse, or uncertainty that makes a direct answer unsafe.
12. When human review is needed, provide an actionable internal note and a short requester-facing draft saying the internal team is reviewing the request.
13. Return only the final JSON matching the provided schema.

Quality bar:
- do not repeat information already present
- do not ask questions that the image or files already answer
- do not claim certainty without evidence
- when evidence is missing for a document-scoped answer, make the uncertainty explicit
- keep public text concise and practical
"""

TRIAGE_OUTPUT_SCHEMA = """{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "ticket_class": {
      "type": "string",
      "enum": ["support", "access_config", "data_ops", "bug", "feature", "unknown"]
    },
    "confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0
    },
    "impact_level": {
      "type": "string",
      "enum": ["low", "medium", "high", "unknown"]
    },
    "requester_language": {
      "type": "string",
      "minLength": 2
    },
    "summary_short": {
      "type": "string",
      "minLength": 1,
      "maxLength": 120
    },
    "summary_internal": {
      "type": "string",
      "minLength": 1
    },
    "development_needed": {
      "type": "boolean"
    },
    "needs_clarification": {
      "type": "boolean"
    },
    "clarifying_questions": {
      "type": "array",
      "maxItems": 3,
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "incorrect_or_conflicting_details": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "evidence_found": {
      "type": "boolean"
    },
    "relevant_paths": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "path": { "type": "string" },
          "reason": { "type": "string" }
        },
        "required": ["path", "reason"]
      }
    },
    "answer_scope": {
      "type": "string",
      "enum": ["document_scoped", "general_reasoning"]
    },
    "evidence_status": {
      "type": "string",
      "enum": ["verified", "not_found_low_risk_guess", "not_applicable"]
    },
    "misuse_or_safety_risk": {
      "type": "boolean"
    },
    "human_review_reason": {
      "type": "string"
    },
    "recommended_next_action": {
      "type": "string",
      "enum": [
        "ask_clarification",
        "auto_public_reply",
        "auto_confirm_and_route",
        "draft_public_reply",
        "route_dev_ti"
      ]
    },
    "auto_public_reply_allowed": {
      "type": "boolean"
    },
    "public_reply_markdown": {
      "type": "string"
    },
    "internal_note_markdown": {
      "type": "string"
    }
  },
  "required": [
    "ticket_class",
    "confidence",
    "impact_level",
    "requester_language",
    "summary_short",
    "summary_internal",
    "development_needed",
    "needs_clarification",
    "clarifying_questions",
    "incorrect_or_conflicting_details",
    "evidence_found",
    "relevant_paths",
    "answer_scope",
    "evidence_status",
    "misuse_or_safety_risk",
    "human_review_reason",
    "recommended_next_action",
    "auto_public_reply_allowed",
    "public_reply_markdown",
    "internal_note_markdown"
  ]
}
"""

TRIAGE_PROMPT_TEMPLATE = """$stage1-triage

Task:
Analyze this internal ticket for Stage 1 triage only.

Constraints:
- Use only the ticket title, ticket messages, attached images, files under manuals/, and files under app/.
- Use manuals/ and app/ only when the request is about internal app behavior, app usage, or an internal process that may be documented there.
- When the question is not document-scoped, you may answer using general reasoning.
- Do not use databases, logs, DDL, schema dumps, or external web search.
- Return only valid JSON matching the provided schema.
- Ask at most 3 clarifying questions.
- Never promise a fix, implementation, or timeline.
- Internal messages may inform internal analysis and routing but MUST NOT be disclosed in automatic public replies unless the same information is already public on the ticket.

Ticket reference:
{REFERENCE}

Ticket title:
{TITLE}

Ticket requester role:
{REQUESTER_ROLE}

Requester can view internal messages:
{REQUESTER_CAN_VIEW_INTERNAL_MESSAGES}

Current status:
{STATUS}

Urgent:
{URGENT}

Public messages:
{PUBLIC_MESSAGES}

Internal messages:
{INTERNAL_MESSAGES}

Decision policy:
- Classify into exactly one of: support, access_config, data_ops, bug, feature, unknown.
- impact_level means business/user impact only.
- development_needed is only a triage estimate.
- Set answer_scope=document_scoped when the requester is asking about internal app behavior, product usage implemented in app/, or an internal process/workflow that should be grounded in manuals/ or app/.
- Set answer_scope=general_reasoning when the question can be answered safely without grounding in manuals/ or app/.
- For document-scoped questions, search the relevant sources before answering.
- If document-scoped evidence is found, set evidence_status=verified.
- If no document-scoped evidence is found but a low-risk best-effort answer is still useful, you may answer or draft a reply, but set evidence_status=not_found_low_risk_guess and clearly say in public_reply_markdown that you could not verify it in manuals/ or app/.
- If manuals/ or app/ are not the right source for the question, set evidence_status=not_applicable.
- misuse_or_safety_risk=true only when a direct answer could enable misuse, create a safety problem, or otherwise should be held for human review.
- Human review is for misuse_or_safety_risk=true or uncertainty that would make a direct answer unsafe.
- If the requester can view internal messages, prefer a direct best-effort answer instead of deferring for human approval.
- Leave internal_note_markdown empty when the public reply is self-contained; use an internal note when escalation or human review context would help another developer.
- If the request is understood and safe to answer directly, use auto_public_reply.
- If the request is understood and should go to Dev/TI after a safe requester update, use auto_confirm_and_route.
- If another requester answer could safely unlock the next step, use ask_clarification.
- If human review is needed, set human_review_reason, provide an actionable internal_note_markdown, and write a short public_reply_markdown draft telling the requester the internal team is reviewing the request.
- If information is ambiguous, missing, conflicting, or likely incorrect, ask concise clarifying questions instead of guessing.
- If you ask clarifying questions, still provide full classification fields and a concise clarification reply suitable to send to the requester.
- If no safe public reply should be prepared, leave public_reply_markdown empty and set auto_public_reply_allowed to false.

Output:
Return only the JSON object.
"""
