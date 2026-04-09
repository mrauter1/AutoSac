# Document Verification Criteria
Check these boxes (`- [x]`) only when the target document itself satisfies the rule.

## Completeness
- [ ] **Implementation-Ready Scope**: The document defines the system purpose, major components, responsibilities, and boundaries clearly enough that an autonomous coding agent would not need to invent the overall design.
- [ ] **Behavior Completeness**: The main flows, edge cases, failure modes, and recovery behavior that materially affect implementation are specified or explicitly declared out of scope.
- [ ] **Interface & Data Contracts**: Every interface, data shape, persisted entity, protocol, file format, and integration needed for implementation is defined with enough precision to code against.
- [ ] **Operational Constraints**: Relevant runtime constraints are stated clearly, including performance, security, observability, configuration, deployment assumptions, and other non-functional requirements that affect implementation.

## Clarity
- [ ] **Ambiguity Control**: The document contains no unresolved placeholders such as TBD/TODO/??? and no materially ambiguous language that would force an implementer to guess.
- [ ] **Internal Consistency**: Sections, examples, tables, and terminology do not contradict each other.

## Economy
- [ ] **Single Source of Truth**: Each requirement or contract has one canonical home. Cross-references, concise summaries, and clearly informative examples are acceptable, but duplicate passages that add no new normative information should not exist.
- [ ] **Appropriate Abstraction Level**: The document specifies contracts, invariants, externally relevant states, interactions, observable artifacts, and constraints without overspecifying one internal implementation strategy. Detail that affects external behavior, persisted state, failure handling, recovery, security, compatibility, migration, or interoperability counts as part of the contract and must be stated when needed.
