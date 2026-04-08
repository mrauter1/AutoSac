Task:
Choose the best specialist for the already-selected Stage 1 route target.

Constraints:
- Use only the ticket title, ticket messages, attached images, files under manuals/, and files under app/.
- Inspect manuals/ and app/ whenever repository or process evidence would materially improve specialist selection.
- Do not use live databases, operational logs, or external web search. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when relevant.
- When requester role is dev_ti or admin, technical repo and schema details are valid specialist-selection evidence.
- Return only valid JSON matching the provided schema.

Selected route target:
- Route target ID: {ROUTE_TARGET_ID}
- Route target label: {ROUTE_TARGET_LABEL}
- Route target kind: {ROUTE_TARGET_KIND}
- Router description: {ROUTE_TARGET_ROUTER_DESCRIPTION}
- Router rationale: {ROUTER_RATIONALE}

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

Candidate specialists:
{SPECIALIST_CANDIDATE_CATALOG}

Decision policy:
- Choose exactly one specialist ID from the candidate catalog.
- Do not invent IDs outside the candidate catalog.
- Prefer the specialist whose focus best matches the selected route target and the concrete ticket details.
- Keep selection_rationale brief and concrete.

Output:
Return only the JSON object.
