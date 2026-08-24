# System and Skill Architecture

## Purpose

PrePR Verify independently decides whether a pending change is ready for a pull request. It reviews actual repository state in a fresh context and treats prior implementation-session claims as neither evidence nor authority.

## Product boundary

V1 is a Codex Skill plus a small deterministic Python core, not a standalone review product and not a prompt-only Skill.

The Skill owns:

- context orchestration;
- semantic impact and risk analysis;
- Spec and Standards reasoning;
- semantic test-sufficiency review;
- evidence-backed finding proposals.

The semantic-assessment layer represents those proposals as a bound assessment through
the deterministic core. The Skill/model supplies semantic status, rationale,
and finding judgments; the core validates axis completeness, evidence-reference
targets through the same canonical loader used for external deserialization,
source precedence comparisons, and identity bindings. It does not
claim that schema validation proves the semantic conclusion.

Semantic artifact bounds, human-report presentation budgets, and runtime
model/context budgets are separate concerns. The semantic-assessment contract bounds only the
canonical persisted assessment. It may inspect complete identity-bound captured
sources progressively and stores short previews plus stable locators; it does
not infer token budgets or provider context windows from artifact characters.
The ReviewArtifact layer owns concise rendering without deleting canonical evidence. Its
versioned `ReviewArtifact` binds the exact ChangeSet, DiscoveryResult,
VerificationPlan, VerificationEvidence, and SemanticAssessment identities and
records verifier version/build identity. It carries bounded review-facing
summaries and the already-bounded semantic findings, never source files, specs,
or command output.

The deterministic core owns:

- Git scope and ChangeSet capture;
- typed models and serialization;
- verification request/result contracts;
- evidence reference integrity;
- schema and cross-field validation;
- axis and final-verdict reduction;
- report rendering from the canonical artifact.

The deterministic core can be tested and reused independently of a live model. Schema validation proves contract consistency, not the truth of semantic reasoning.

## Lifecycle boundary

A readiness review exists only after invocation and repository-comparison preflight succeeds and produces a reviewable, non-empty scope. Invalid invocation, a non-Git target, an invalid explicit base, or failure to establish a merge base/comparison scope is a preflight failure. It returns CLI exit code `3` and produces no `READY`, `NEEDS_CHANGES`, or `INCONCLUSIVE` verdict.

After scope establishment, missing or unreliable required evidence is part of an actual review. It produces `INCONCLUSIVE` and exit code `2` unless a confirmed blocker already yields `NEEDS_CHANGES`.

`capture` has a narrower lifecycle: it may successfully serialize an empty ChangeSet with `empty = true` and exit code `0`. A full review presented with that ChangeSet stops as `nothing_to_review` with exit code `3`; it does not create axes or a readiness verdict. Code `3` therefore covers preflight/no-review scope outcomes, not a readiness result.

## V1 workflow

```text
Explicit repository and base (possibly materialized by human-attached setup)
  -> preflight and deterministic ChangeSet
  -> reject nothing_to_review
  -> requirement and Standards evidence discovery
  -> impact and risk proposal
  -> deterministic verification floor
  -> sandbox-aware verification execution
  -> semantic review
  -> structured evidence
  -> deterministic axis/verdict reduction
  -> Markdown report
```

V1 supports macOS/Linux Git repositories and is language-agnostic. Windows behavior is deferred. V1 does not depend on GitHub, a webhook, a GitHub App, PR comments, or inline mapping.

PrePR Verify fixes the portable review workflow, evidence, verdict, and safety skeleton. Repository and company requirements, canonical verification commands, security tooling, affected-scope conventions, and monorepo practices enter that skeleton as discovered evidence or explicitly trusted policy; they are not encoded as language or framework matrices in the product.

## Versioned data contracts

ChangeSet and ReviewArtifact are independent versioned contracts. ChangeSet describes captured repository state, while ReviewArtifact describes completed review evidence, axes, reduction, and reporting. Each has its own schema version and release lifecycle; neither uses a project-wide global `schema_version`.

## Read-only meaning

V1 is a non-authoring reviewer. It may create caches and build artifacts only in a disposable review snapshot. It must not change the author's original working tree, index, HEAD, or Git history. Verification that unexpectedly mutates the author repository is unreliable and constitutes a reviewer-boundary failure.

## Progressive disclosure

`SKILL.md` contains the user-facing V1 workflow and routes to relevant contracts.
`AGENTS.md` adds repository-development invariants and the same concise verdict
boundary. Detailed contracts live in task-specific numbered documents and are
read only when their stage is active. No future-version module or empty
abstraction is created merely to mirror a planned architecture.

The Scope Intent Resolver is Skill orchestration support, not a second scope
contract. It discovers bounded choices, requires an explicit human or
automation selection, pins the result to an immutable base commit, and previews
Git metadata before full source capture or semantic loading. Confirmation is
the boundary between metadata-only setup and the unchanged deterministic
ChangeSet. The ChangeSet and ReviewArtifact contracts remain the only canonical
readiness boundaries.

The deterministic `PreReviewSetup` coordinator is likewise orchestration
support. It owns only bounded choice records, the ordered
`SCOPE -> REQUIREMENTS -> VERIFICATION -> FINAL_CONFIRMATION -> READY_TO_REVIEW`
state transitions, cancellation, and the readiness guard. The Skill owns all
rendering and conversation. Existing capture, discovery, execution capability,
semantic, and reduction contracts remain the only owners of their respective
decisions and artifacts.
