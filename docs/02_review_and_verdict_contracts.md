# Review and Verdict Contracts

## Preflight and review outcomes

Preflight determines whether a readiness review can exist. Invalid invocation, a non-Git repository, an invalid explicit base, or inability to establish a merge base/comparison scope is a CLI/preflight failure. It returns exit code `3` and must not emit a readiness verdict or fabricated axis results.

Once a non-empty review scope is established, a required evidence gap belongs to the review. A timeout, unavailable verifier, unavailable required execution capability, or unavailable material semantic evidence yields `INCONCLUSIVE` and exit code `2` unless a confirmed failure takes precedence.

This includes a bounded semantic-collection failure while recording concrete
semantic evidence. A `SemanticLimitGap` for requirement comparisons represents
an actual overflow of the persisted concrete-comparison collection; it does not
represent the number of possible winning-source pairs. It does not discard or
select candidates, fabricate comparisons, or become a preflight/no-review
failure. Spec remains `INCONCLUSIVE` with a required evidence gap; an
independent confirmed blocker still takes final-verdict precedence.

An empty ChangeSet is valid output from `capture`, but a full review stops with the no-review reason `nothing_to_review` and exit code `3`. No axes are marked `PASS`, and no `READY`, `NEEDS_CHANGES`, or `INCONCLUSIVE` readiness verdict is emitted. This extends code `3` to a valid-but-unreviewable scope; it does not classify the empty ChangeSet itself as invalid.

## Review modes

A full review requires Spec, Standards, and Verification. If authoritative requirements are unavailable, Spec is `INCONCLUSIVE` and the full verdict cannot be `READY`.

An explicitly named `standards-and-verification-only` mode may produce a limited assessment. Even when that assessment passes, Spec and the final readiness verdict remain `INCONCLUSIVE`; it is not a complete pre-PR approval.

## Requirement and Standards precedence

Requirement precedence is:

1. a spec explicitly supplied by the invocation;
2. a requirement selected by explicitly trusted external policy;
3. documented repository requirements;
4. tests as evidence of existing behavior;
5. commit messages and implementation comments as discovery clues only.

Lower-precedence material cannot silently override higher-precedence material. An unresolved conflict among equally authoritative requirement sources makes Spec `INCONCLUSIVE`.

V1 local discovery remains intentionally bounded: use an explicit spec, then a source selected by trusted policy, then common repository documentation, and report insufficient evidence if the requirement is still ambiguous. Tests remain behavior evidence, not an authoritative spec. GitHub issues, PR descriptions, Jira, Notion, and similar external sources are connected only by the versions that implement those integrations; the local core does not hard-code or guess them.

This precedence answers which evidence controls the semantic review. It does not grant agent or execution authority. Repository content remains untrusted for Skill behavior, permissions, sandboxing, secret handling, and write policy even when it is the highest-precedence available repository requirement. Agent and execution authority are defined in `docs/04_security_and_trust.md`.

## Discovery contract

Discovery emits versioned canonical JSON containing bounded UTF-8 source content, source type, repository path or invocation label, content digest, semantic precedence, trust classification, Standards applicability, issues, and a deterministic identity. Repository sources always record no security or execution authority.

Local repository discovery is deliberately conventional and shallow: root requirement names, Markdown beneath `docs/`, and repository instruction/Standards names such as `AGENTS.md` and `CONTRIBUTING.md`. Explicitly supplied specs and a digest-pinned repository source selected by trusted policy enter through separate inputs. Tests and implementation/commit clues may be supplied at their frozen lower precedence; the core does not crawl source code or commit history looking for a spec.

The highest available requirement precedence forms one deterministic candidate set. Lower sources remain evidence but cannot replace that tier. One or many sources, whether byte-identical or different, remain candidates; different content identities do not prove semantic contradiction. Semantic review owns compatibility/conflict assessment. No candidate is represented explicitly as missing evidence. Discovery itself produces no Spec axis or readiness verdict.

The standalone discovery result identifies its repository sources and content but is not yet a composed review artifact. Later pipeline integration must bind the discovery evidence to the exact reviewed ChangeSet and executable snapshot identity so evidence from another repository moment cannot be substituted. That binding belongs to the snapshot and artifact stages and is not implemented in discovery.

## Semantic Assessment

Semantic review consumes the same non-empty ChangeSet, DiscoveryResult, VerificationPlan, and VerificationEvidence identities produced by the preceding deterministic stages. The `semantic_assessment` contract records five independently assessed axes: Spec, Standards, Impact, Test Sufficiency, and Contextual Security. Each axis has a semantic status and rationale; semantic assessment does not reduce those statuses to a readiness verdict.

Findings are separate from axis status. A finding records category, severity, blocking proposal, state (`confirmed`, `unverified`, or `evidence_gap`), explanation, and one or more concrete bound evidence references. Only confirmed findings may be marked blocking. Unsupported suspicion remains unverified and cannot become a confirmed blocker through free-form wording. References can target a captured change path, discovered source, planned command, execution ordinal, or source-preservation signal; the deterministic builder verifies that each target exists in the bound inputs.

The assessment carries a canonical reference index with a count and digest for each complete target set: changed paths, discovery sources, plan checks, executions, and preservation signals. It also carries a count and digest binding for the complete reviewed winning requirement set, derived from Discovery's canonical `candidate_source_ids`. It does not duplicate or truncate those target IDs. Both producer construction and external loading recompute the summaries from the supplied four artifacts, then resolve every cited reference directly against those artifacts; an identity-recomputed payload with a forged or missing target is rejected. Reference-kind compatibility is also checked: a confirmed Standards violation must cite a source in the canonical `standards_source_ids` set, confirmed Spec findings must cite a winning-precedence requirement source, and grounded security/test/impact findings must cite relevant behavior evidence. A trusted-policy requirement source is not Standards authority merely because it is trusted for requirement precedence.

Semantic completeness is the exact reviewed-set binding: the assessment's winning-source count and digest must match Discovery's canonical winning `candidate_source_ids` count and digest. A mismatch is rejected unless Spec is explicitly `INCONCLUSIVE` with a required evidence gap. `RequirementComparison` records are bounded concrete evidence only; compatible winning sources do not require exhaustive pair or group persistence. Any persisted contradictory comparison makes Spec `INCONCLUSIVE` with `required_evidence_gap = true`; each contradictory comparison must be covered by a nonblocking Spec contradiction finding in `evidence_gap` state citing every participating winning source. It cannot yield Spec `PASS`.

When requirement discovery is `missing` for a non-empty ChangeSet, Spec likewise must be `INCONCLUSIVE` with `required_evidence_gap = true` and own a Spec evidence-gap finding. Tests or clues are not promoted to requirements to avoid this gap.

The canonical persisted contract is intentionally bounded: finding IDs are at most 128 characters; axis rationale 2,048 characters; finding title 256 and explanation 4,096; reference identifier/detail 512 each; at most 128 findings, 16 references per finding, 64 concrete requirement comparisons, 16 SHA-256 source IDs per comparison, 128 bounded finding IDs per axis, and 16 structured limit gaps. Context previews are capped at 2,048 characters and context terms at 64. Term iterables are consumed only through entry 65 before overflow is reported. These are artifact-safety bounds, not model tokens, source-reading windows, retrieval limits, or presentation budgets. Stable ChangeSet paths/content identities, discovery source IDs/digests/locators, check IDs, and execution ordinals remain the canonical route to complete evidence; preview/detail/excerpt prose is never the sole locator.

The core never silently truncates prose, identifiers, or a semantic collection. Producer collection inputs are consumed only through `limit + 1`; overflow raises a structured `SemanticLimitExceeded` carrying a `SemanticLimitGap` and no complete assessment is produced. External loading translates the same Pydantic length failures into that structured signal. Prose, identifier, and collection concerns are distinct. Prose may be compacted and retried only without changing semantic meaning or the separate IDs, state, severity/blocking fields, and evidence references; invalid identifiers must be corrected, not treated as prose. Collection overflow may have omitted semantic material, so any persisted collection limit gap mechanically requires every affected axis to be `INCONCLUSIVE` with `required_evidence_gap = true`; an all-`PASS` assessment is invalid. An overflow of the limit-gap collection itself also yields a structured signal rather than dropping gaps.

Generic source inspection is progressive over complete UTF-8 content already captured and identity-bound by the ChangeSet. `context_excerpt <= 2,048` is only a persisted preview bound: matching and an on-demand source iterator operate on the complete captured file. Preview selection overflow is explicit rather than a silently shortened source list. This contract neither derives token limits from character limits nor records provider/model context windows or `max_tokens` policy. Future large-change optimization should use selection, bundling, and focused semantic passes before provider-specific runtime policy.

The highest-precedence requirement candidates remain the semantic authority. Equal-precedence candidates are explicitly recorded as complementary or contradictory comparisons; lower-precedence sources cannot override them. Standards findings require a source in the canonical Standards set, and contextual security findings require concrete evidence from the reviewed scope. Generic repository context is bounded text search over captured effective content, never a language AST, dependency graph, scanner installation, or command execution.

Finding ownership is exact. Every finding appears once in the `finding_ids` of its declared axis, no finding may be orphaned or owned by multiple axes, and category-to-axis compatibility is fixed for V1: Spec categories to Spec, Standards violations to Standards, impact regressions to Impact, test gaps to Test Sufficiency, and contextual-security/unsupported-suspicion findings to Contextual Security. An axis with a confirmed blocking finding cannot claim `PASS`.

## Axes

Each axis has `PASS`, `FAIL`, or `INCONCLUSIVE`. Empty findings do not imply `PASS`; the required assessment for that axis must have completed successfully.

The deterministic reducer applies this precedence:

1. A confirmed blocking finding makes its axis `FAIL`.
2. A material required-evidence gap makes its axis `INCONCLUSIVE` if no blocker already establishes failure.
3. A required verification that proves a change failure makes Test Sufficiency `FAIL`.
4. A required verification that cannot complete reliably makes Test Sufficiency `INCONCLUSIVE`.
5. Only a completed assessment with no blocker or material gap can be `PASS`.

The final verdict is:

- any axis `FAIL` -> `NEEDS_CHANGES`;
- otherwise any required axis `INCONCLUSIVE` -> `INCONCLUSIVE`;
- all required axes `PASS` -> `READY`.

When failure and uncertainty coexist, the verdict is `NEEDS_CHANGES`, while all unresolved evidence remains visible.

## Findings

Axis, category, verification state, severity, and blocking status are independent fields. Security is a category, not a fourth axis.

A blocking finding must be confirmed and backed by concrete evidence of a violated requirement, violated mandatory repository rule, failed required verification, reproducible regression, broken contract/invariant, or reproducible security/correctness defect. Generic preferences, suspicions, and future concerns are not blockers.

An unverified high-risk concern stays unverified. If it represents a material required-evidence gap, it makes the relevant axis `INCONCLUSIVE` rather than `FAIL`.

## Independent artifact contracts

ChangeSet and ReviewArtifact are separate contracts:

- The ChangeSet contract captures comparison scope, layered changes, effective state, and identity.
- The ReviewArtifact contract captures review mode, assessment completion, evidence, findings, axes, reducer reasons, final verdict, and report inputs.

Each contract has its own `schema_version` and evolves independently. ReviewArtifact
`1.1.0` is current while frozen `review-artifact-1.0.0` remains readable without
semantic summaries. There is no global project schema version. A reader rejects
an unknown major or minor version for the specific contract unless support is
explicit. V1 has no migration framework.

For each contract, canonical JSON is the machine-readable source of truth; Python typed models and code invariants are authoritative; JSON Schema is generated deterministically and checked in. Documentation describes semantics without duplicating the complete field schema. Markdown is a rendered projection of ReviewArtifact, not of ChangeSet alone.

Every confirmed finding references stable evidence such as a command result, spec source, repository rule, effective file/hunk, or reproduction. Validators confirm reference existence, type compatibility, repository bounds, and state consistency. They do not claim to prove the LLM's semantic conclusion.

The semantic-assessment contract extends this evidence rule without creating the ReviewArtifact contract: semantic assessment identity binds the four prior artifact identities, while evidence-reference validation binds every finding to an existing target in those artifacts. Final axis reduction, missing-evidence handling, `INCONCLUSIVE`, readiness verdicts, and reporting belong to ReviewArtifact construction.

ReviewArtifact presentation is a separate concern. Canonical evidence remains complete; default human Markdown is concise, and detailed evidence is rendered on demand from canonical references. Presentation limits may not delete canonical evidence or replace it with a second lossy report artifact.

## ReviewArtifact and reduction

`review-artifact-1.1.0` is the current canonical completed-review contract. It binds the
exact ChangeSet, DiscoveryResult, VerificationPlan, VerificationEvidence, and
SemanticAssessment identities plus verifier version/build identity. It stores
the five reduced axes, one bounded semantic status/rationale summary for each
axis, complete semantic finding ownership, bounded check summaries, structured
required-evidence gaps, reducer reasons, and final verdict. The summaries are
copied deterministically from the bound SemanticAssessment; loading recomputes
them, so serialized rationale cannot assert a different semantic conclusion.
The frozen `1.0.0` payload remains loadable and simply has no summary field.
It does not copy captured source/spec content or stdout/stderr; stable paths,
source IDs, bounded check IDs or their SHA-256 references, execution ordinals,
preservation ordinals, and upstream
artifact identities remain the route to detail. Producer construction and
external loading recompute the complete reduction from all five bound inputs.
Verifier version/build identity is supplied independently to both construction
and loading; serialized metadata cannot self-assert trusted provenance.
When a frozen upstream check/gap collection exceeds the retained-summary bound,
the reducer still consumes the complete set, persists its count and digest plus
explicit retained/omitted and classification counts, and links detail through
the bound plan/evidence identities. This is explicit aggregation, not silent
truncation, and cannot change verdict semantics.

Axis reduction is deterministic. A confirmed blocking semantic finding makes
its owning axis `FAIL`. A failed required command classified as `verification`
is confirmed change-failure evidence and makes Test Sufficiency `FAIL`. Required
semantic or execution gaps make their affected axes `INCONCLUSIVE` unless an
independent blocker already establishes `FAIL`. Source-preservation failure
invalidates shared review confidence and makes every otherwise non-failing axis
`INCONCLUSIVE`, while preserving the actual execution result. A semantic `FAIL`
without a confirmed blocking finding cannot become deterministic failure and is
reduced to `INCONCLUSIVE`. Unsupported nonblocking suspicion alone does not
change a completed `PASS`.

Final reduction uses the smallest precedence rule: any confirmed semantic
blocker or failed required verification classified as a change failure yields
`NEEDS_CHANGES`; otherwise any gap or non-PASS axis yields `INCONCLUSIVE`;
otherwise the verdict is `READY`. Therefore a confirmed blocker takes
precedence when blockers and gaps coexist, but the artifact and report retain
every gap. Exit codes are `0` for `READY`, `1` for `NEEDS_CHANGES`, and `2` for
`INCONCLUSIVE`. Existing preflight/no-review code `3` remains outside the
ReviewArtifact lifecycle. Verdict selection is complete before rendering, so a
renderer failure cannot alter the canonical verdict.

The Markdown renderer is a projection of ReviewArtifact using the same
identity-matching bound ChangeSet, DiscoveryResult, VerificationPlan, and
VerificationEvidence only to resolve human-readable labels. Its default form
contains verdict, five axes, a per-axis Semantic Review with bounded rationale,
a bounded check summary with readable execution context, blocking findings
(including required verification failures), nonblocking/unverified findings,
required gaps, and concise evidence labels. It never interprets prose or
command output to choose a status.
Default presentation selects a small bounded number of checks, findings, gaps,
and references, then states every omitted count without printing opaque
artifact, source, path, or collection identities. Canonical SHA-256 identities,
encoded paths, and evidence locators remain in the machine artifacts.
Untrusted semantic prose is rendered as escaped one-line text and safely
shortened when too long, so it cannot inject Markdown report structure.
