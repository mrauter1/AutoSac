# Update Verification Criteria
Check these boxes (`- [x]`) only when the target document itself satisfies the rule for this update request.

## Completeness
- [ ] **Requested Changes Applied**: Every requested change in `.docloop/update_request.md` is reflected in the target document clearly and completely.
- [ ] **No Unintended Regression Against Baseline**: Requirements and contracts from `.docloop/update_baseline.md` that were not meant to change are still present and compatible, or any removal/change is explicitly justified by the update request.
- [ ] **Breaking Change Handling**: Any breaking change, compatibility impact, migration need, or behavior removal introduced by the update is stated explicitly enough that implementers will not miss it.
- [ ] **Interface & Data Contracts**: Every interface, data shape, persisted entity, protocol, file format, and integration touched by the update is defined with enough precision to code against.
- [ ] **Operational Constraints**: Relevant runtime constraints introduced or affected by the update are stated clearly, including performance, security, observability, configuration, deployment assumptions, and other non-functional requirements that affect implementation.

## Clarity
- [ ] **Ambiguity Control**: The updated document contains no unresolved placeholders such as TBD/TODO/??? and no materially ambiguous language that would force an implementer to guess, especially around the requested changes.
- [ ] **Internal Consistency**: Updated sections, unchanged sections, examples, tables, and terminology do not contradict each other.

## Economy
- [ ] **Single Source of Truth**: Updated requirements and contracts have one canonical home. Cross-references, concise summaries, and clearly informative examples are acceptable, but the update must not introduce duplicate passages that add no new normative information.
- [ ] **Appropriate Abstraction Level**: The update preserves contract-level detail and does not introduce unnecessary implementation-specific algorithm choices, local sequencing, or code-structure guidance. Detail that affects external behavior, persisted state, failure handling, recovery, security, compatibility, migration, or interoperability remains part of the contract and must stay explicit when needed.
