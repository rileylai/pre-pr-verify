# Review and Verdict Contracts

## Preflight and review outcomes

Preflight determines whether a readiness review can exist. Invalid invocation, a non-Git repository, an invalid explicit base, or inability to establish a merge base/comparison scope is a CLI/preflight failure. It returns exit code `3` and must not emit a readiness verdict or fabricated axis results.

Once a non-empty review scope is established, a required evidence gap belongs to the review. A timeout, unavailable verifier, unavailable required execution capability, or unavailable material semantic evidence yields `INCONCLUSIVE` and exit code `2` unless a confirmed failure takes precedence.

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

## Milestone 1.3 discovery contract

Discovery emits versioned canonical JSON containing bounded UTF-8 source content, source type, repository path or invocation label, content digest, semantic precedence, trust classification, Standards applicability, issues, and a deterministic identity. Repository sources always record no security or execution authority.

Local repository discovery is deliberately conventional and shallow: root requirement names, Markdown beneath `docs/`, and repository instruction/Standards names such as `AGENTS.md` and `CONTRIBUTING.md`. Explicitly supplied specs and a digest-pinned repository source selected by trusted policy enter through separate inputs. Tests and implementation/commit clues may be supplied at their frozen lower precedence; the core does not crawl source code or commit history looking for a spec.

The highest available requirement precedence forms one deterministic candidate set. Lower sources remain evidence but cannot replace that tier. One or many sources, whether byte-identical or different, remain candidates; different content identities do not prove semantic contradiction. Milestone 1.5 owns semantic compatibility/conflict assessment. No candidate is represented explicitly as missing evidence. Discovery itself produces no Spec axis or readiness verdict.

The standalone milestone 1.3 result identifies its repository sources and content but is not yet a composed review artifact. Later pipeline integration must bind the discovery evidence to the exact reviewed ChangeSet and executable snapshot identity so evidence from another repository moment cannot be substituted. That binding belongs to the later snapshot/artifact milestones and is not implemented in discovery.

## Axes

Each axis has `PASS`, `FAIL`, or `INCONCLUSIVE`. Empty findings do not imply `PASS`; the required assessment for that axis must have completed successfully.

The deterministic reducer applies this precedence:

1. A confirmed blocking finding makes its axis `FAIL`.
2. A material required-evidence gap makes its axis `INCONCLUSIVE` if no blocker already establishes failure.
3. A required verification that proves a change failure makes Verification `FAIL`.
4. A required verification that cannot complete reliably makes Verification `INCONCLUSIVE`.
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

- The ChangeSet contract begins in milestone 1.2 and captures comparison scope, layered changes, effective state, and identity.
- The ReviewArtifact contract begins in milestone 1.6 and captures review mode, assessment completion, evidence, findings, axes, reducer reasons, final verdict, and report inputs.

Each contract has its own `schema_version`, initially `"1.0.0"`, and evolves independently. There is no global project schema version. A reader rejects an unknown major or minor version for the specific contract unless support is explicit. V1 has no migration framework.

For each contract, canonical JSON is the machine-readable source of truth; Python typed models and code invariants are authoritative; JSON Schema is generated deterministically and checked in. Documentation describes semantics without duplicating the complete field schema. Markdown is a rendered projection of ReviewArtifact, not of ChangeSet alone.

Every confirmed finding references stable evidence such as a command result, spec source, repository rule, effective file/hunk, or reproduction. Validators confirm reference existence, type compatibility, repository bounds, and state consistency. They do not claim to prove the LLM's semantic conclusion.
