# Architecture Decision Log

## ADR-001: Skill plus deterministic core

**Status:** Accepted

Use a Codex Skill for semantic judgment and orchestration, backed by a small independently testable Python core for Git, models, execution contracts, evidence, validation, and reduction. A standalone product and prompt-only implementation are both outside V1.

## ADR-002: Complete pending state with explicit base

**Status:** Accepted

V1 requires an explicit base and preserves committed, staged, unstaged, and non-ignored untracked origins. It creates a deterministic effective identity without pretending the layers are one three-dot diff.

## ADR-003: Fail-closed three-axis verdict

**Status:** Accepted

Spec, Standards, and Verification remain distinct. Any confirmed failure yields `NEEDS_CHANGES`; otherwise missing required evidence yields `INCONCLUSIVE`; only completed passing assessments yield `READY`.

## ADR-004: Repository content is evidence, not authority

**Status:** Accepted

Repository material may define requirements and conventions but cannot grant execution/write permission or weaken security policy. Trusted repository policy requires explicit user approval and digest recording.

## ADR-005: Isolation, not executable allowlists

**Status:** Accepted

Any repository-aware command may execute repository code. Verification safety depends on non-waivable execution invariants, honest host capabilities, disposable snapshots, bounded processes, and secret removal. Approval may accept only a policy-designated, explicitly disclosed capability gap; it cannot waive core invariants.

## ADR-006: Versioned JSON contracts

**Status:** Accepted

Typed Python models and code invariants are authoritative. They generate checked-in JSON Schemas, and Markdown is rendered from canonical ReviewArtifact JSON. Unknown schema major and minor versions fail closed unless explicitly supported.

## ADR-007: Python and minimal dependencies

**Status:** Accepted

Use Python 3.11+, uv with `uv_build`, Pydantic v2, pytest, argparse, and standard-library subprocess. Avoid redundant CLI and schema frameworks.

## ADR-008: Strict milestone boundaries

**Status:** Accepted

Foundation contains documentation, project configuration, and a smoke harness only. ChangeSet models and capture begin in the next independently verified milestone. V2/V3 implementation decisions remain deferred to their versions.

## ADR-009: Preflight is not a readiness verdict

**Status:** Accepted

Invocation and comparison-scope failures return exit code `3` without a readiness verdict. Only gaps encountered after a non-empty review scope exists can produce review-level `INCONCLUSIVE`. Empty capture succeeds, while full review stops as `nothing_to_review` with code `3` and without fabricated axes.

## ADR-010: ChangeSet and ReviewArtifact evolve independently

**Status:** Accepted

ChangeSet begins in milestone 1.2 and ReviewArtifact in milestone 1.6. Each has independent typed models, generated schema, invariants, and schema version; there is no project-wide global artifact version.

## ADR-011: Safety approval has a bounded waiver scope

**Status:** Accepted

Core execution and authority invariants cannot be waived by repository config or human approval. Policy may allow explicit human risk acceptance for a disclosed host capability gap, but approval never converts an unsafe command or missing capability into a safe one.

## ADR-012: Dogfood progressively and bootstrap from independent evidence

**Status:** Accepted

Each milestone dogfoods only the capability it actually implements. A complete self-review first exists after 1.6 but is only additional release evidence until 1.7. Future candidates should be reviewed by a last-known-good verifier, and candidate self-review is never the sole trusted gate.
