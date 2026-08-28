# Architecture Decision Log

## ADR-001: Skill plus deterministic core

**Status:** Accepted

Use a Codex Skill for semantic judgment and orchestration, backed by a small independently testable Python core for Git, models, execution contracts, evidence, validation, and reduction. A standalone product and prompt-only implementation are both outside V1.

## ADR-002: Complete pending state with explicit base

**Status:** Accepted

V1 requires an explicit base and preserves committed, staged, unstaged, and non-ignored untracked origins. It creates a deterministic effective identity without pretending the layers are one three-dot diff.

## ADR-003: Fail-closed five-axis verdict

**Status:** Accepted

Spec, Standards, Impact, Test Sufficiency, and Contextual Security remain distinct, while Verification remains separate evidence. Any confirmed failure yields `NEEDS_CHANGES`; otherwise missing required evidence yields `INCONCLUSIVE`; only completed passing assessments yield `READY`.

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

## ADR-008: Strict implementation boundaries

**Status:** Accepted

Foundation contains documentation, project configuration, and a smoke harness only. ChangeSet models and capture follow in an independently verified stage. V2/V3 implementation decisions remain deferred to their versions.

## ADR-009: Preflight is not a readiness verdict

**Status:** Accepted

Invocation and comparison-scope failures return exit code `3` without a readiness verdict. Only gaps encountered after a non-empty review scope exists can produce review-level `INCONCLUSIVE`. Empty capture succeeds, while full review stops as `nothing_to_review` with code `3` and without fabricated axes.

## ADR-010: ChangeSet and ReviewArtifact evolve independently

**Status:** Accepted

ChangeSet and ReviewArtifact have independent typed models, generated schemas, invariants, and schema versions; there is no project-wide global artifact version.

## ADR-011: Safety approval has a bounded waiver scope

**Status:** Accepted

Core execution and authority invariants cannot be waived by repository config or human approval. Policy may allow explicit human risk acceptance for a disclosed host capability gap, but approval never converts an unsafe command or missing capability into a safe one.

## ADR-012: Dogfood progressively and bootstrap from independent evidence

**Status:** Accepted

Each capability stage dogfoods only what it actually implements. A complete self-review exists only once the full local pipeline is available, and it remains additional release evidence until independent validation is complete. Future candidates should be reviewed by a last-known-good verifier, and candidate self-review is never the sole trusted gate.

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

Once a valid ChangeSet and plan establish review scope, inability to materialize complete effective content is execution evidence, not a new capture/preflight failure. The executor records an explicitly incomplete, file-less SnapshotManifest and a matching non-executable `ExecutionRequest`/`ExecutionResult` with a structured capability, configuration, or permission cause; required checks retain `required_evidence_gap`. Incomplete manifests cannot expose partial executable files, and no process starts from them.

## ADR-018: Preserve executed results across late source-preservation failure

**Status:** Accepted

Snapshot materialization failure before a child starts produces the existing `NOT_RUN` evidence gap. If the final source recapture detects repository mutation after a child has already run, the executor retains that command's actual status, exit code, and bounded output, then adds a separately bound `SourcePreservationFailure` signal with a required evidence gap. No late preservation failure is rewritten as a process skip, and no final verdict reducer is added here.

## ADR-019: Bounded literal lookahead redaction

**Status:** Accepted

Streaming explicit-value redaction uses a bounded literal lookahead matcher that visits every input offset, finds the union of all protected intervals (including different-offset and self-overlapping matches), and retains only a bounded tail across process-output chunks and truncation boundaries. If a boundary cannot be proven safe, the excerpt is suppressed. If the replacement marker overlaps any protected pattern, or pattern limits are exceeded, redaction fails closed by suppressing excerpts. A preservation-failure signal may bind only to an execution whose status is not `NOT_RUN`.

## ADR-020: Bound semantic assessment to deterministic evidence

**Status:** Accepted

Semantic judgment is represented as a separate `semantic_assessment-1.0.0` contract bound to the exact ChangeSet, DiscoveryResult, VerificationPlan, and VerificationEvidence identities. It records Spec, Standards, Impact, Test Sufficiency, and Contextual Security assessments plus evidence-backed confirmed, unverified, or evidence-gap findings. Only confirmed findings may be blocking. Equal-precedence requirement candidates are explicitly classified as complementary or contradictory; lower-precedence evidence cannot override the winning tier. The semantic layer performs bounded generic context over captured content and never executes repository commands. Axis reduction, readiness status, ReviewArtifact, and reporting remain separate deterministic stages.

## ADR-021: Reference-oriented assessment bounds and report boundary

**Status:** Accepted

Semantic assessment loading uses a canonical reference index derived from the four bound deterministic artifacts. Producer and external deserialization use the same validator; identity recomputation alone is insufficient. Reference kinds must be capable of supporting the cited finding. The frozen 1.0.0 contract requires pair/group comparison coverage; current 1.1.0 instead binds the complete reviewed winning-source set separately and treats comparisons as bounded concrete evidence. Empty ChangeSets stop at `nothing_to_review`. Assessment free text and collections are bounded so the contract cannot become a second source/log artifact. ADR-022 refines the index representation and separates these artifact bounds from presentation and runtime context budgets.

The Markdown renderer renders the human report from canonical `ReviewArtifact` JSON. The default report is concise: verdict, axis statuses, checks, finding summaries, and references. Detailed evidence is expandable or on demand; long reasoning and evidence are not duplicated into a second report artifact. This ADR does not change the reducer or ReviewArtifact contract.

## ADR-022: Separate artifact, presentation, and runtime context budgets

**Status:** Accepted

Semantic-assessment character/count limits bound only canonical persisted artifacts. They neither cap relevant captured source inspection nor imply provider tokens or model context windows. Complete UTF-8 source remains available progressively from the bound ChangeSet; the 2,048-character context excerpt is a preview backed by path and content identity. Large-change runtime work should prefer selection, bundling, and focused semantic passes before provider-specific token policy.

No semantic prose or collection is silently truncated. Limit failures are structured as prose or semantic-collection concerns. Prose may be compacted and retried only with its semantic structure and evidence bindings unchanged. A collection gap requires affected axes to remain inconclusive, and an overflow cannot produce a five-axis pass. The canonical reference index uses complete-set counts and digests instead of a bounded second copy of target IDs; actual reference existence is revalidated against the bound artifacts.

The frozen 1.0.0 comparison contract has group semantics: one comparison classifies the complete cited group, covers all pairs within it, rejects ambiguous overlaps, and rejects missing pair coverage. Current 1.1.0 preserves the same bounded record shape and contradiction integrity but no longer uses pair/group coverage as the review-completeness invariant; complete reviewed-set count and identity bind that invariant. ReviewArtifact owns concise presentation only: presentation limits may select detail for display but never delete canonical evidence. This decision adds no reducer, renderer, `ReviewArtifact`, model token limit, or provider policy.

## ADR-023: Fail-closed semantic axis ownership and authority sets

**Status:** Accepted

The semantic-assessment contract mechanically binds each finding to exactly one compatible axis. Orphan, duplicate, cross-axis, category-incompatible, and `PASS`-with-confirmed-blocker states are invalid. Finding IDs are bounded to 128 characters, axis references use the same bound, and comparison source identifiers are fixed SHA-256 IDs. Identifier overflow is classified separately from prose and collection overflow. Generic context consumes no more than 65 term entries before reporting the 64-entry collection limit.

Requirement reconciliation also constrains Spec evidence. Missing requirements and contradictory winning comparisons require an inconclusive Spec evidence gap; each contradictory group cites its participating winning sources through a Spec contradiction gap finding. This is semantic evidence validation, not final verdict reduction. Standards authority is the explicit canonical `standards_source_ids` set. A trusted requirement selection does not become a Standards source by source type or trust label alone.

## ADR-024: Deterministic ReviewArtifact reduction and report projection

**Status:** Accepted

The `review-artifact-1.0.0` contract is bound to the exact five prior
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

Release validation proves the frozen V1 chain with real-repository deterministic
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

## ADR-028: Numbered pre-review setup above frozen V1 contracts

**Status:** Accepted

The next Skill usability patch keeps interaction in the orchestration/runbook
layer. Human-attached setup uses stable numbered scope choices, a nested bounded
base/feature-start chooser, an explicit requirements decision, an explicit
verification authorization decision, and one final confirmation. Enter accepts
only a displayed recommendation and therefore remains an explicit human action.

Requirement-source acknowledgement does not promote repository prose, remove
same-precedence candidates, or alter the frozen precedence/comparison rules.
Repository-native verification declarations remain candidates and never grant
execution permission. Human approval is materialized only through the existing
trusted `ExecutionCapability` / `approval_waivable` / `approved_gaps` inputs;
non-waivable invariants and reducer semantics are unchanged.

Interactive setup defaults to disposable snapshots, network off, and external
services off. Headless mode receives the numeric-equivalent structured choices
and explicit final confirmation; it never prompts or waits. Missing scope,
requirements, or execution configuration remains a preflight failure or an
existing evidence gap. This adds no schema, CLI, policy engine, provider/model
management, V2, or V3 behavior.

## ADR-029: Executable setup guard, post-scope semantic gaps, and installed identity

**Status:** Accepted

The documentation protocol is backed by a small deterministic
`PreReviewSetup` coordinator. It exposes bounded numbered choice records,
validates numeric/Enter input, owns only the five ordered setup phases plus
cancellation, and rejects canonical review until final confirmation. Rendering,
prompting, ChangeSet capture, requirement discovery, execution authorization,
semantic judgment, and reduction remain with their existing owners.

Once a non-empty ChangeSet exists, a real bounded concrete-comparison overflow
is a structured `SemanticLimitGap`, not preflight/no-review. It requires Spec
`INCONCLUSIVE`, while review completeness is independently checked through the
complete winning-set binding. Missing reviewed candidates cannot be hidden by
the comparison bound. Existing reducer precedence remains unchanged.

Installed verifier provenance uses a bounded deterministic SHA-256 over the
installed Python core. It works in copied Skills and wheels without `.git`,
does not inspect the target repository or claim an unavailable Git SHA, and is
still supplied independently to ReviewArtifact construction/loading. This
changes no versioned schema or verifier version.

## ADR-030: Bounded independent Git repository execution profile

**Status:** Accepted

Dogfood exposed a legitimate compatibility gap: a repository-wide verification
command could be correct for the reviewed change while its internal Git use
failed inside the historical `.git`-free filesystem snapshot. That historical
snapshot remains valid for `FILESYSTEM_ONLY`. v0.1.5 therefore adds an explicit
`GIT_REPOSITORY` profile alongside `FILESYSTEM_ONLY`, resolved monotonically
from repository declarations, trusted policy, model proposals, and user
invocation.

`GIT_REPOSITORY` is a fresh independent standalone repository per command. It
has its own Git metadata, object database, `HEAD`, index, working tree, and
sanitized configuration. Linked worktrees, shared metadata, alternates,
hardlink dependencies, and inherited author Git authority remain rejected.
The profile provides bounded HEAD/index/working-tree/untracked semantics, not
arbitrary history or ancestry, tags, remotes, reflogs, submodules, LFS, or
author-specific Git configuration behavior. Omitted content and gitlinks remain
explicit materialization gaps.

Source Git reads use `GIT_OPTIONAL_LOCKS=0`; source preservation remains
fail-closed. This protects Git authority and source-repository linkage but does
not claim that the ordinary subprocess adapter is an OS-level hostile-code
filesystem sandbox. Such a sandbox is not introduced by this decision.

`VerificationPlan` and `VerificationEvidence` 1.1.0 bind the resolved profile;
1.0.0 artifacts remain frozen legacy contracts. The bounded direct-Git gate is
limited to the documented supported argv forms and rejects unsupported direct
Git or repository/config escape forms before execution. Indirect commands remain
opaque and retain their real child outcomes. Existing ChangeSet, reducer,
capability, budget, and authority-separation semantics are unchanged. History,
tags, remotes, submodules, and LFS remain deferred.

## ADR-031: Bounded requirement relevance above frozen discovery

**Status:** Accepted

When discovery returns many same-precedence requirement candidates, the
Skill/orchestration layer may derive a small presentation recommendation from
the already captured ChangeSet and bound DiscoveryResult. The recommendation is
deterministic and generic: it uses bounded lexical overlap from changed paths
and safely captured text against candidate path, label, and bounded content,
with path matches ranked above label matches, label matches ranked above body
matches, and canonical candidate order as the tie-break. It returns at most five
source IDs and has no persisted artifact or authority semantics.

The complete winning `candidate_source_ids`, precedence, DiscoveryResult
identity, and semantic equal-precedence reconciliation remain unchanged. The
coordinator retains the full candidate tuple and count, labels recommended
entries as inspection context, and never turns a recommendation into an
implicit selection. Repository prose, including prompt-injection text, is data
used only by the bounded presentation heuristic; it cannot grant authority,
execution permission, or reducer behavior. No language/framework matrix,
embedding search, dependency graph, or external search service is introduced.
When the winning set exceeds the presentation bound, recommended entries are
shown first and unused slots up to five total are filled from canonical
candidate order without a recommendation marker. A zero-recommendation
overflow therefore still exposes the first canonical candidates for inspection;
the complete candidate set and its count remain unchanged.

## ADR-032: Bound semantic rationale to the canonical review artifact

**Status:** Accepted

The Skill performs a bounded senior-style inspection before constructing the
semantic assessment. It records one concise rationale for each materially
reviewed axis and proposes only evidence-grounded findings; verification results
remain separate evidence and green checks never substitute for semantic review.

ReviewArtifact `1.1.0` carries the five semantic status/rationale summaries
copied from the bound `SemanticAssessment`. Construction and external loading
recompute the summaries from the same bound inputs, so serialized rationale
cannot self-assert a different conclusion. Markdown renders only this canonical
artifact, escapes or digests untrusted prose, and retains the existing bounded
finding/gap/reference presentation. Frozen ReviewArtifact `1.0.0` remains
loadable without the new summaries. No second report artifact, model benchmark,
AST/dependency engine, scanner installation, or execution authority is added.

## ADR-034: Host-portable Agent Skill distribution

**Status:** Accepted

The Skill/core boundary is host-portable. OpenAI Codex and Claude Code use one
canonical root `SKILL.md`; Codex installs and invokes it through
`~/.codex/skills/pre-pr-verify` and `$pre-pr-verify`, while Claude Code uses
`~/.claude/skills/pre-pr-verify` and `/pre-pr-verify`. Distribution and
invocation syntax differ, but semantic judgment remains host-model supplied
and the deterministic contracts remain provider-independent.

The verifier resolves its owned resources and locked Python runtime from the
loaded Skill root, not from a host-name directory or the target repository.
No provider runtime abstraction, SDK, model identity, or host adapter is
introduced. `CLAUDE.md` is added only as a conventional target-repository
Standards source alongside existing standards files; it remains untrusted
evidence with no security, execution, or verdict authority. Cross-host release
evidence requires fresh forward-tests for both supported hosts, while candidate
self-review remains supplemental.

## ADR-033: Bind complete winning-set reviewability separately from comparisons

**Status:** Accepted

SemanticAssessment `1.1.0` records `reviewed_requirement_sources` as the
count-and-identity binding of Discovery's canonical winning
`candidate_source_ids`. Producer construction and loading require an exact
match, or an explicit Spec `INCONCLUSIVE` required-evidence gap. This proves
review completeness without persisting every compatible pair or overlapping
group. `RequirementComparison` remains a bounded collection of concrete
compatibility or contradiction evidence; an actual collection overflow still
creates a Spec `SemanticLimitGap`. The frozen `1.0.0` loader and schema retain
their pair/group coverage semantics. No Discovery, ReviewArtifact, reducer,
presentation, runtime-context, retrieval, batching, scheduler, or comparison
database framework is added.
