---
name: triage-router
description: Route a Stage 1 ticket to the correct specialist without drafting the final public reply.
---

Use this skill when:
- the task is the first routing pass for a Stage 1 internal ticket
- the workspace contains app/ and manuals/
- the output must be a small JSON routing object

Hard rules:
1. Stage 1 is read-only.
2. Do not modify files under app/ or manuals/.
3. Do not inspect live databases or operational logs. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed when they materially improve routing accuracy.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, public attachment files listed in the prompt and available via workspace paths, files under manuals/, and files under app/.
6. Inspect relevant prompt-listed attachment files, manuals/, and app/ whenever repository or process evidence would materially improve routing accuracy.
7. Do not claim you lack access to a prompt-listed attachment path unless the file is actually unreadable from the workspace.
8. When requester role is dev_ti or admin, technical repo and schema details are valid routing evidence.
9. Choose exactly one enabled route_target_id from the prompt catalog.
10. Keep the rationale concise and evidence-based.
11. Return only the final JSON object that matches the provided schema.
