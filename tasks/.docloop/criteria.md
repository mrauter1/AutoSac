# Document Verification Criteria
Check these boxes (`- [x]`) only when the target document itself satisfies the rule.

## Completeness
- [x] **Implementation-Ready Scope**: The document defines the system purpose, major components, responsibilities, and boundaries clearly enough that an autonomous coding agent would not need to invent the overall design.
- [ ] **Behavior Completeness**: The main flows, edge cases, failure modes, and recovery behavior that materially affect implementation are specified or explicitly declared out of scope.
- [ ] **Interface & Data Contracts**: Every interface, data shape, persisted entity, protocol, file format, and integration needed for implementation is defined with enough precision to code against.
- [ ] **Operational Constraints**: Relevant runtime constraints are stated clearly, including performance, security, observability, configuration, deployment assumptions, and other non-functional requirements that affect implementation.

## Clarity
- [ ] **Ambiguity Control**: The document contains no unresolved placeholders such as TBD/TODO/??? and no materially ambiguous language that would force an implementer to guess.
- [ ] **Internal Consistency**: Sections, examples, tables, and terminology do not contradict each other.

## Economy
- [x] **Single Source of Truth**: Each requirement or contract has one canonical home. Cross-references, concise summaries, and clearly informative examples are acceptable, but duplicate passages that add no new normative information should not exist.
- [x] **Appropriate Abstraction Level**: The document specifies contracts, invariants, externally relevant states, interactions, observable artifacts, and constraints without overspecifying one internal implementation strategy. Detail that affects external behavior, persisted state, failure handling, recovery, security, compatibility, migration, or interoperability counts as part of the contract and must be stated when needed.

## Current Blockers
- Section 8.2 says `webhook_url` is any non-empty string, but Section 8.3 says invalid webhook values must make Slack config globally invalid and pause delivery. The PRD must define the canonical validity rule for `webhook_url` and related routing config, including whether a malformed or non-HTTPS URL and a missing or blank `SLACK_DEFAULT_TARGET_NAME` under enabled Slack are global-invalid-config cases. Without that, different engineers can reasonably choose between pausing all rows unchanged versus attempting or dead-lettering rows individually.
- Section 8.3 requires logging invalid Slack configuration, but Section 11 only makes invalid-config logging explicit in the worker runtime and only requires emission-path logs to say whether a target row was created. The PRD must define the canonical observability contract when invalid config suppresses target creation during emission: either require an emission-path config-error log/reason, or explicitly assign all invalid-config logging to one shared validator/process so operators do not have to guess why no target row was created.
