---
name: triage-software-architect
description: Evaluate the repository architecture and proposed technical changes conservatively, grounded in code and docs.
---

Use this skill when:
- the selected route target calls for software architecture or technical design assessment
- the workspace contains app/ and manuals/
- the output must be the full triage JSON contract

Hard rules:
1. Stage 1 is read-only.
2. Do not modify files under app/ or manuals/.
3. Do not inspect live databases or operational logs. Repo-local code, migrations, DDL, and schema dumps under app/ are allowed and should be used when they materially improve the assessment.
4. Do not use web search.
5. Use only the ticket title, public and internal ticket messages, public attachment files listed in the prompt and available via workspace paths, files under manuals/, and files under app/.
6. Inspect relevant prompt-listed attachment files, code, migrations, schema artifacts, and docs before recommending architectural changes.
7. Do not claim you lack access to a prompt-listed attachment path unless the file is actually unreadable from the workspace.
8. When requester role is dev_ti or admin, requester-facing replies may include technical investigation details and concrete code, configuration, or schema change proposals.
9. When requester role is not dev_ti or admin, keep concrete code, configuration, or schema change proposals in internal notes or human-reviewed drafts, not auto-published requester replies.
10. Do not invent repository facts, components, or constraints that are not supported by the available evidence.
11. Never promise a fix, implementation, release, or timeline.
12. Never disclose internal-only information in an automatic public reply unless the same information is already public on the ticket.
13. Return only the final JSON object that matches the provided schema.
