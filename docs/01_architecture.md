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
Explicit repository and base
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

## Versioned data contracts

ChangeSet and ReviewArtifact are independent versioned contracts. ChangeSet begins in milestone 1.2 and describes captured repository state. ReviewArtifact begins in milestone 1.6 and describes completed review evidence, axes, reduction, and reporting. Each has its own schema version and release lifecycle; neither uses a project-wide global `schema_version`.

## Read-only meaning

V1 is a non-authoring reviewer. It may create caches and build artifacts only in a disposable review snapshot. It must not change the author's original working tree, index, HEAD, or Git history. Verification that unexpectedly mutates the author repository is unreliable and constitutes a reviewer-boundary failure.

## Progressive disclosure

`AGENTS.md` contains only navigation and hard invariants. Detailed contracts live in task-specific numbered documents. No future-version module or empty abstraction is created merely to mirror a planned architecture.
