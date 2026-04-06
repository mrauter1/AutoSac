---
name: triage-specialist-selector
description: Select the best Stage 1 specialist from the allowed candidates for a chosen route target.
---

Use this skill when:
- the route target has already been chosen
- the prompt provides a bounded candidate specialist catalog
- the output must be a small JSON selection object

Hard rules:
1. Stage 1 is read-only.
2. Do not modify files under app/ or manuals/.
3. Do not inspect databases, DDL, schema dumps, or logs.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, attached images, files under manuals/, and files under app/.
6. Search manuals/ and inspect app/ only when the request depends on internal app behavior, app usage, or an internal process that may be documented there.
7. Choose exactly one specialist ID from the provided candidate catalog.
8. Keep the rationale concise and evidence-based.
9. Return only the final JSON object that matches the provided schema.
