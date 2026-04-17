Task:
Route this internal ticket to the correct Stage 1 route target.

Constraints:
- Use only the ticket title, ticket messages, public attachments listed below, files under manuals/, and files under app/.
- Inspect relevant prompt-listed attachment files, manuals/, and app/ whenever repository or process evidence would materially improve routing accuracy.
- Do not claim you lack access to a prompt-listed attachment path unless the file is actually unreadable from the workspace.
- When the question is not document-scoped, you may still classify using general reasoning.
- Do not use live databases, operational logs, or external web search. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when relevant.
- When requester role is dev_ti or admin, technical repo and schema details are valid routing evidence.
- Return only valid JSON matching the provided schema.

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

Attachment workspace root:
{ATTACHMENTS_ROOT}

Public attachments:
{PUBLIC_ATTACHMENTS}

Routing policy:
- Use only route_target_id values from the enabled catalog below.
- Do not invent route_target_id values outside the catalog.
- Prefer the most specific enabled route target supported by the evidence.
- When requester role is dev_ti or admin, prefer software_data_engineer for asks that explicitly want implementation text, repo-grounded code diffs, patch output, or concrete full-file code.
- Prefer software_architect for design review, migration strategy, technical assessment, or choosing between implementation approaches.
- Prefer business_analyst for scope framing, viability checks, requirement clarification, or pushing back on weak solution framing before implementation.
- Keep routing_rationale brief and concrete.

Enabled route targets:
{ROUTE_TARGET_CATALOG}

Output:
Return only the JSON object.
