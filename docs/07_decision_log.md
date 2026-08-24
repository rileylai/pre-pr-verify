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

## ADR-020: Bound semantic assessment to deterministic evidence

**Status:** Accepted

Milestone 1.5 represents semantic judgment as a separate `semantic_assessment-1.0.0` contract bound to the exact ChangeSet, DiscoveryResult, VerificationPlan, and VerificationEvidence identities. It records Spec, Standards, Impact, Test Sufficiency, and Contextual Security assessments plus evidence-backed confirmed, unverified, or evidence-gap findings. Only confirmed findings may be blocking. Equal-precedence requirement candidates are explicitly classified as complementary or contradictory; lower-precedence evidence cannot override the winning tier. The semantic layer performs bounded generic context over captured content and never executes repository commands. Axis reduction, readiness status, ReviewArtifact, and reporting remain 1.6.

## ADR-021: Reference-oriented assessment bounds and 1.6 report boundary

**Status:** Accepted

Semantic assessment loading uses a canonical reference index derived from the four bound deterministic artifacts. Producer and external deserialization use the same validator; identity recomputation alone is insufficient. Reference kinds must be capable of supporting the cited finding, and all winning equal-precedence requirement candidates require pair/group comparison coverage. Empty ChangeSets stop at `nothing_to_review`. Assessment free text and collections are bounded so the contract cannot become a second source/log artifact. ADR-022 refines the index representation and separates these artifact bounds from presentation and runtime context budgets.

Milestone 1.6 will render the human report from canonical `ReviewArtifact` JSON. The default report is concise: verdict, axis statuses, checks, finding summaries, and references. Detailed evidence is expandable or on demand; long reasoning and evidence are not duplicated into a second report artifact. This ADR does not implement the reducer, ReviewArtifact, or renderer.

## ADR-022: Separate artifact, presentation, and runtime context budgets

**Status:** Accepted

Milestone 1.5 character/count limits bound only canonical persisted semantic artifacts. They neither cap relevant captured source inspection nor imply provider tokens or model context windows. Complete UTF-8 source remains available progressively from the bound ChangeSet; the 2,048-character context excerpt is a preview backed by path and content identity. Large-change runtime work should prefer selection, bundling, and focused semantic passes before provider-specific token policy.

No semantic prose or collection is silently truncated. Limit failures are structured as prose or semantic-collection concerns. Prose may be compacted and retried only with its semantic structure and evidence bindings unchanged. A collection gap requires affected axes to remain inconclusive, and an overflow cannot produce a five-axis pass. The canonical reference index uses complete-set counts and digests instead of a bounded second copy of target IDs; actual reference existence is revalidated against the bound artifacts.

Requirement comparisons have group semantics. One comparison classifies the complete cited group and covers all pairs within it; overlaps are ambiguous and rejected, and missing pair coverage is rejected. Thus a 16-source winning set can be completely reconciled by one group comparison and does not require raising the 64-comparison bound. Milestone 1.6 owns concise presentation only: presentation limits may select detail for display but never delete canonical evidence. This decision adds no reducer, renderer, `ReviewArtifact`, model token limit, or provider policy.

## ADR-023: Fail-closed semantic axis ownership and authority sets

**Status:** Accepted

Milestone 1.5 mechanically binds each finding to exactly one compatible axis. Orphan, duplicate, cross-axis, category-incompatible, and `PASS`-with-confirmed-blocker states are invalid. Finding IDs are bounded to 128 characters, axis references use the same bound, and comparison source identifiers are fixed SHA-256 IDs. Identifier overflow is classified separately from prose and collection overflow. Generic context consumes no more than 65 term entries before reporting the 64-entry collection limit.

Requirement reconciliation also constrains Spec evidence. Missing requirements and contradictory winning comparisons require an inconclusive Spec evidence gap; each contradictory group cites its participating winning sources through a Spec contradiction gap finding. This is semantic evidence validation, not the 1.6 final reducer. Standards authority is the explicit canonical `standards_source_ids` set. A trusted requirement selection does not become a Standards source by source type or trust label alone.

## ADR-024: Deterministic ReviewArtifact reduction and report projection

**Status:** Accepted

Milestone 1.6 introduces `review-artifact-1.0.0`, bound to the exact five prior
artifact identities and verifier version/build identity. The canonical loader
recomputes the artifact from those inputs, so a recomputed identity cannot forge
bindings, finding ownership, check classification, axis status, gaps, or verdict.
The artifact keeps complete bounded semantic findings plus reference-oriented
check/gap summaries, never source/spec bodies or process output.
Frozen upstream verification collections may exceed the artifact's retained
summary budget. Reduction therefore evaluates the complete collections and
stores bounded selections alongside complete-set counts/digests, explicit
omission counts, and blocker/gap classifications. Verifier provenance is an
independent loader input rather than self-asserted serialized metadata.

Confirmed semantic blockers and failed required verification classified as a
change failure produce `NEEDS_CHANGES`, even when uncertainty also exists.
Otherwise any required gap or non-PASS axis produces `INCONCLUSIVE`; only five
PASS axes and complete required verification produce `READY`. Required execution
gaps affect Test Sufficiency. A post-execution preservation failure remains
separate from its actual result and invalidates all axis confidence. Structural
floor entries have no execution by design and are summarized as satisfied unless
their validated preservation signal records a gap.

Markdown rendering is a pure projection of the canonical artifact. Exit mapping
is determined from the artifact verdict (`0`, `1`, `2`) before rendering;
preflight/no-review remains code `3` and creates no ReviewArtifact. This adds no
command execution, runtime/model orchestration, provider policy, retrieval
engine, language analysis, or V2/V3 integration.
The default renderer applies its own smaller presentation selection, reports
omitted counts and stable identities, and escapes untrusted prose onto one line
so semantic text cannot forge headings or verdict text.

## ADR-025: V1 release distribution and acceptance boundary

**Status:** Accepted

The repository checkout is the Codex Skill distribution: root `SKILL.md` is the
concise entrypoint and routes progressively to the exact canonical API sequence
in `docs/09_v1_skill_runbook.md`, then to stage-specific numbered contracts,
checked-in schemas, and the locked Python project. The wheel and sdist distribute the
deterministic Python core; they do not pretend to be a standalone semantic
reviewer. Full review invocation remains through the Skill, while the public CLI
continues to expose deterministic capture only.

Milestone 1.7 proves the frozen V1 chain with real-repository deterministic
acceptance fixtures, clean package installation/import, schema and Skill
validation, documentation consistency, self-dogfood, and a fresh independent
Skill/release review. The semantic forward-test uses the available Codex Skill
context and fixed safety rubric; it adds no provider API keys, token accounting,
runtime orchestration, network dependency, or V2/V3 behavior.

## ADR-026: Explicit Scope Intent Resolver above the frozen ChangeSet

**Status:** Accepted

The post-V1 usability patch keeps scope setup in Skill orchestration support.
Human-attached use may discover bounded base candidates and first-parent recent
commits, recommend an intent, and preview deterministic scope metadata, but it
must receive an explicit selection before capture. Working changes pin HEAD;
current branch pins the selected candidate; an inclusive feature-start choice
pins its first parent; custom pins the supplied ref. Headless missing input is a
preflight failure and never prompts. Every completed choice becomes the existing
explicit repository/base/`pending` ChangeSet input; no default branch, hidden
boundary, reducer rule, or report behavior is added.

Large or mixed-scope detection is advisory only. Review focus may prioritize
inspection but cannot filter the ChangeSet or restrict callers, tests, adjacent
code, and contracts available to the reviewer. A future setup display may show
canonical requirement discovery and trusted execution-policy provenance, but
display does not change requirement precedence or grant execution authority.

## ADR-027: Metadata-only setup and unambiguous custom refs

**Status:** Accepted

Pre-selection discovery and preview use only hardened, bounded Git ref,
first-parent history, commit-count, and path/status metadata. They do not call
canonical capture or open working-tree/index blobs. Because content-derived line
statistics would cross that boundary, setup reports them unavailable rather
than fabricating an estimate. Explicit confirmation authorizes the first full
canonical ChangeSet capture; this is orchestration sequencing, not a new scope
implementation.

Custom input accepts a full commit SHA, a fully qualified ref, or a short name
that exists in exactly one of Git's deterministic ref namespaces. Multiple
namespace matches are ambiguity even when they point at the same commit.
Invalid refs and refs that cannot peel to a commit fail preflight. Successful
resolution remains SHA-pinned. When working changes exist and the first
plausible branch candidate exceeds the existing large commit/path threshold
with a broader path set, recommendation favors working changes without selecting
it; no scoring engine or reducer rule is introduced.
