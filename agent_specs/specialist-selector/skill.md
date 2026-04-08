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
3. Do not inspect live databases or operational logs. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when they materially improve specialist selection.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, attached images, files under manuals/, and files under app/.
6. Inspect manuals/ and app/ whenever repository or process evidence would materially improve specialist selection.
7. When requester role is dev_ti or admin, technical repo and schema details are valid specialist-selection evidence.
8. Choose exactly one specialist ID from the provided candidate catalog.
9. Keep the rationale concise and evidence-based.
10. Return only the final JSON object that matches the provided schema.
