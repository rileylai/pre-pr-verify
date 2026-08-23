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

## ADR-013: Fixed review skeleton with repository-native inputs

**Status:** Accepted

PrePR Verify owns the portable workflow, evidence, verdict, and non-waivable safety invariants. Requirements, canonical commands, scanners, impact conventions, and monorepo affected tooling are discovered from the repository or supplied by explicitly trusted policy. V1 does not embed language command/AST matrices, auto-install scanners, infer enterprise policy, or build a monorepo dependency engine; unresolved scope or required execution remains visible as an evidence gap.

Deterministic discovery selects a precedence tier and preserves its complete source candidate set. Content differences are not semantic-conflict evidence; that judgment belongs to semantic review. Later composed evidence must bind discovery to the reviewed ChangeSet/snapshot identity.

## ADR-014: Direct disposable snapshots and minimal evidence binding

**Status:** Accepted

Materialize a fresh verification tree for each planned command directly from captured HEAD objects plus ChangeSet effective blobs, without clone checkout, repository filters, linked worktrees, or the author's Git metadata. Executable trees contain no `.git`; unsupported omitted content and gitlinks fail closed. Bind plan, discovery, canonical-guidance digests, deterministic materialization ordinal, execution request, capability, re-derived decision, and result through ChangeSet/discovery/snapshot identities. Repository/planner/trusted-policy input channels assign origin rather than accepting a caller-supplied authority label. The deterministic floor consists of structural invariants proven by artifact/control-flow validation, not commands missing results. These constraints provide moment consistency without introducing a provenance database, filesystem transaction engine, command dependency graph, or policy engine. Plain `subprocess` remains only a bounded process adapter, never a claimed sandbox.

## ADR-015: Reproducible scope and bounded process lifetime

**Status:** Accepted

Explicit ignored-path includes are serialized in ChangeSet identity and reused by every consistency recapture. Execution deadlines cover process-group lifetime and pipe draining, including descendants that inherit stdout/stderr; no collector thread is allowed to extend a request indefinitely. Runtime cwd validation returns classified `NOT_RUN` evidence. SnapshotFile accepts only regular/symlink states with compatible modes. These are contract-level invariants, not a new sandbox or execution framework.

## ADR-016: Version the reproducible ChangeSet scope

**Status:** Accepted

The established `changeset-1.0.0` schema and identity remain frozen as a legacy reader. Explicit ignored-path scope is a semantic addition, so capture emits `changeset-1.1.0` and `schemas/changeset-1.1.0.schema.json`; the loader supports only 1.0.0 and 1.1.0 without a migration framework. Invalid external include paths are rejected at model validation. Execution decisions carry a structured blocked failure kind so serialized `NOT_RUN` evidence cannot relabel capability, configuration, or permission causes. Snapshot manifests reject duplicate paths.

## ADR-017: Record post-capture snapshot evidence gaps explicitly

**Status:** Accepted

Once a valid ChangeSet and plan establish review scope, inability to materialize complete effective content is execution evidence, not a new capture/preflight failure. 1.4 records an explicitly incomplete, file-less SnapshotManifest and a matching non-executable `ExecutionRequest`/`ExecutionResult` with a structured capability, configuration, or permission cause; required checks retain `required_evidence_gap`. Incomplete manifests cannot expose partial executable files, and no process starts from them.

## ADR-018: Preserve executed results across late source-preservation failure

**Status:** Accepted

Snapshot materialization failure before a child starts produces the existing `NOT_RUN` evidence gap. If the final source recapture detects repository mutation after a child has already run, 1.4 retains that command's actual status, exit code, and bounded output, then adds a separately bound `SourcePreservationFailure` signal with a required evidence gap. No late preservation failure is rewritten as a process skip, and no final verdict reducer is added here.

## ADR-019: Bounded literal lookahead redaction

**Status:** Accepted

Streaming explicit-value redaction uses a bounded literal lookahead matcher that visits every input offset, finds the union of all protected intervals (including different-offset and self-overlapping matches), and retains only a bounded tail across process-output chunks and truncation boundaries. If a boundary cannot be proven safe, the excerpt is suppressed. If the replacement marker overlaps any protected pattern, or pattern limits are exceeded, redaction fails closed by suppressing excerpts. A preservation-failure signal may bind only to an execution whose status is not `NOT_RUN`.
