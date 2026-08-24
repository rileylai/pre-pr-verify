# V1 Skill Runbook

This is the executable handoff between the root `SKILL.md` and the frozen V1
Python contracts. Use it for a full local review. The standalone CLI only
captures a `ChangeSet`; it is not a full-review shortcut.

## Canonical imports

```python
from pre_pr_verify import __version__
from pre_pr_verify.build_identity import installed_core_identity
from pre_pr_verify.discovery import ProvidedRequirement, discover_review_sources
from pre_pr_verify.executor import execute_verification_plan
from pre_pr_verify.git_capture import capture_changeset
from pre_pr_verify.models import ScopeMode
from pre_pr_verify.review import (
    build_review_artifact,
    load_review_artifact,
    render_markdown_report,
    verdict_exit_code,
)
from pre_pr_verify.semantic import (
    bind_semantic_reference,
    build_semantic_assessment,
    load_semantic_assessment,
)
from pre_pr_verify.semantic_models import (
    EvidenceReferenceKind,
    FindingCategory,
    FindingSeverity,
    FindingState,
    RequirementComparison,
    RequirementRelation,
    SemanticAxis,
    SemanticAxisAssessment,
    SemanticFinding,
    SemanticLimitGap,
    SemanticStatus,
)
from pre_pr_verify.scope_intent import (
    AdvisoryAction,
    ScopeIntent,
    build_scope_preview,
    capture_resolved_scope,
    discover_scope_options,
    recommend_scope,
    resolve_scope_selection,
)
from pre_pr_verify.pre_review_setup import (
    PreReviewSetup,
    RequirementCandidate,
    SetupPhase,
)
from pre_pr_verify.verification import (
    PlannerCheckInput,
    TrustedPolicyCheckInput,
    build_verification_plan,
    discover_canonical_checks,
)
from pre_pr_verify.verification_models import CapabilityName, ExecutionCapability
```

`ProvidedRequirement`, `TrustedPolicyCheckInput`, and `PlannerCheckInput` are
optional trusted inputs. Repository content may supply evidence and canonical
check candidates, but it cannot grant permission or execution capabilities.

## Scope Intent Resolver

Run setup only in a human-attached session unless the invocation already
supplies an explicit intent and all inputs needed by that intent. Discovery is
bounded and advisory:

- **Working changes:** the user selects `working-changes`; pin the current HEAD
  commit as the base, then capture staged, unstaged, and non-ignored untracked
  state. No committed change is included.
- **Current branch:** show the bounded `base_candidates` from
  `discover_scope_options(...)`. The user must select one candidate. The
  resolver pins its resolved commit SHA; candidate ordering or recommendation
  never selects it.
- **Since commit:** show the bounded first-parent `recent_commits`. A displayed
  commit means "include this feature-start commit and later commits"; the
  resolver pins its first parent as the explicit base. Root commits are not
  offered because they have no base representable by the V1 ChangeSet contract.
- **Custom:** require the user or trusted automation configuration to supply an
  advanced base/ref. Accept a full commit SHA, a fully qualified ref, or a short
  ref name that exists in exactly one Git namespace. Reject invalid refs,
  non-commit objects, and branch/tag or other namespace ambiguity, then pin the
  successful result to a commit SHA.

Use `recommend_scope(options)` only to explain which intent appears useful.
Its result deliberately has `resolved_scope=None`. `recommend != infer`.
`resolve_scope_selection(...)` returns only explicit `ScopeMode.PENDING`
inputs. Missing interactive choices raise `ScopeSelectionRequired`, allowing
the host to ask the human. Cancellation raises `ScopeSelectionCancelled` and
ends preflight with no review. With `interactive=False`, any missing intent,
base candidate, feature-start commit, or custom ref raises `PreflightError`
immediately; never wait for input.

Before selection, `discover_scope_options(...)` uses only hardened Git
path/status metadata: bounded refs and history, commit counts, committed/staged/
unstaged/untracked path sets, and their unions. It must not call
`capture_changeset`, materialize source, or open index/working-tree blobs.
`recommend_scope(options)` recommends working changes when they exist and the
first plausible branch candidate exceeds the existing large commit/path
thresholds while containing a broader path set. Otherwise it may recommend the
branch intent. The reason is advisory and `resolved_scope` remains `None`.

After an explicit intent/boundary choice, call `build_scope_preview(resolved)`.
Show its pinned boundary/base, commit count, changed-path count, and committed/
staged/unstaged/untracked path counts before discovery or semantic inspection.
Content-free setup reports approximate added/deleted lines as unavailable
(`None`, `line_estimate_complete=False`) instead of reading blobs or inventing
line counts. Fixed
large-scope thresholds and a selected scope that combines committed and working
changes produce an advisory only. Offer the returned actions: continue full
scope, use working changes, choose a feature-start commit, or cancel. Continuing
calls `capture_resolved_scope(resolved)` for the first full canonical ChangeSet
capture. Other choices restart metadata setup. The capture rechecks pinned HEAD;
the advisory never changes ReviewArtifact input or reduction.

`review_focus` is an optional repository-relative inspection hint on the
resolved setup record. Do not pass it as a ChangeSet include/exclude boundary.
The reviewer may inspect any relevant callers, tests, adjacent code, and
contracts within the selected review, and readiness remains bound to the full
ChangeSet.

### Numbered interactive setup

Create one `PreReviewSetup` before rendering the first menu. After scope capture
and discovery, call `set_requirement_candidates(...)` with the complete winning
set (source ID plus bounded label). Render only `current_step()` data and feed
each numeric/Enter answer to `submit(...)`. The coordinator has no input or
prompt function: in headless mode, pass structured answers directly. After the
explicit scope is resolved and its preview is accepted, call
`setup.bind_scope(resolved_scope)` before capture to bind the existing scope
resolver identity without recapturing a ChangeSet.
Before starting canonical semantic review, always call
`require_ready_to_review(current_scope=resolved_scope)`; it rejects every phase
except `READY_TO_REVIEW`, rejects cancellation, and rejects a stale repository
or scope. Do not mutate its phase or bypass the guard.

The Skill's human-facing setup uses stable numeric choices rather than asking
the user to repeat scope labels. Display this top-level menu in this order:

```text
1. Working changes                 Recommended (when recommended)
2. Current branch
3. Since commit
4. Custom
```

The user may answer `1`, `2`, `3`, or `4`. If a recommendation is displayed,
Enter is also valid and means explicit human confirmation of that recommendation;
it is not inference. A blank answer without a recommendation is invalid. For
`Current branch`, display a second bounded numbered menu from
`options.base_candidates` and pass only the selected candidate to
`resolve_scope_selection`. Do not flatten base candidates into the top-level
menu. `Since commit` gets the same treatment from `recent_commits`; `Custom`
still requires its explicit SHA/ref text. Re-prompt invalid answers only within
a bounded attempt count, then raise `PreflightError`.

The scope choice and any required advisory action are explicit before calling
`capture_resolved_scope`. Preserve the existing metadata-only preview and
advisory semantics. A recommendation, candidate ordering, or advisory never
changes the ChangeSet boundary.

### Requirement setup

After scope confirmation and capture, but before semantic inspection, call
`discover_review_sources` and show its authoritative winning requirement
candidate set and precedence in a bounded numbered chooser. The actions are:

1. accept one discovered winning source for inspection;
2. enter brief explicit acceptance criteria;
3. continue without authoritative requirements, with a visible warning that
   Spec will remain `INCONCLUSIVE`;
4. cancel.

If there are multiple winning candidates, accepting one is a UI acknowledgement,
not a replacement of the candidate set or a precedence change. Semantic review
must still compare every winning candidate as required by the frozen contract.
Explicit criteria are passed as `ProvidedRequirement` and discovery is reloaded
at explicit precedence. Do not infer requirements from implementation code,
tests, or comments merely to obtain `PASS`. A candidate set that cannot be
displayed within the bounded setup limit must not be silently truncated; offer
explicit criteria, an explicit missing-requirements decision, or cancel.
`PreReviewSetup` exposes the complete candidate count and an overflow flag in
this case, presents no misleading candidate subset, and exposes only those
three legal actions. The full discovery candidate set remains unchanged.

### Verification authorization setup

Discover repository-native commands with `discover_canonical_checks` and show
the bounded local `VerificationPlan` plus its security profile. Repository
content supplies candidate commands and provenance only; it never supplies
execution authority. Offer:

1. explicitly authorize the proposed local checks;
2. review without execution;
3. customize authorization;
4. cancel.

Materialize approval only through the existing `ExecutionCapability` input.
`approved_gaps` may contain a missing capability only when trusted policy first
listed it in `approval_waivable` and the human explicitly approved it. Custom
authorization may select only those disclosed, policy-waivable gaps; it cannot
make a capability `available` or waive a non-waivable invariant. Review without
execution leaves command evidence absent/not-run and therefore preserves the
existing `INCONCLUSIVE` path. The default profile is disposable snapshots,
network off, and external services off unless an explicit trusted policy
authorizes otherwise.

### Final confirmation and headless mode

When the three setup areas resolve, summarize exactly `Scope`, `Requirements`,
and `Verification policy`, then require one final affirmative confirmation
before canonical review starts. Blank is not confirmation; cancellation stops
before semantic review.

For a representative narrow local review with a working-scope recommendation
and one discovered requirement source, the normal answers are `1` (scope), `1`
(requirement), `1` (verification authorization), then `yes`.

With `interactive=False`, never call an input function or wait. Require the
structured equivalents of the scope choice/base, requirement decision, and
verification authorization, plus explicit final confirmation. Missing scope is
a preflight failure. Missing requirement input remains a preflight failure when
required; an explicit continue-without decision is instead the existing Spec
evidence gap. Missing execution configuration remains an explicit capability or
evidence gap. Do not invent a default branch, requirement, approval, or
execution permission.

## Launch and input defaults

From the Skill checkout, run `uv sync --locked`, then launch a review driver
with `uv run python /path/to/driver.py`. A packaged installation may use that
environment's Python directly. Do not launch with an unrelated repository
Python: this is a src-layout package, and the author repository is not an
environment or authority source.

Use these V1 defaults unless a trusted invocation or host policy supplies a
different value:

- `trusted_policy_checks=()` and `planner_additions=()`. Add values only from
  their named trusted channels; repository-discovered checks use
  `discover_canonical_checks` instead.
- `timeout_seconds=300` and `output_limit_bytes=65_536` per command. A trusted
  invocation may lower either bound.
- `redaction_values=()` only when the caller supplied no explicit secret values.
  Otherwise pass those exact values; V1 does not infer secret formats.
- `findings=()`, `requirement_comparisons=()`, and `limit_gaps=()` only when
  inspection establishes that each collection is empty. The five axis values
  have no default. Multiple winning requirement candidates require explicit
  complete comparisons, and missing requirements require the frozen Spec gap.

`required_capabilities` comes only from trusted host/invocation policy, never
from the author repository. For an ordinary local command, use:

```python
required_capabilities = (CapabilityName.OUTPUT_LIMITS,)
```

Add `NETWORK_ISOLATION`, `RESOURCE_LIMITS`, or `PROCESS_ISOLATION` only when a
trusted execution policy explicitly requires that capability for the command or
review. Do not infer optional requirements from the complete `CapabilityName`
enum. If trusted policy requires an unavailable capability, the command remains
blocked unless that specific gap is first listed in `approval_waivable` and the
human explicitly approves it through `approved_gaps`; approval never makes the
capability available or waives a non-waivable invariant.

`verifier_build` identifies the installed PrePR Verify core, not the author
repository. Normal released/copied Skill and wheel installations use
`installed_core_identity()`, a bounded SHA-256 identity over the installed
Python core. It does not inspect `.git` or the target repository, is stable for
identical installed content, and changes with core content. A trusted release
runner may instead supply an installer-recorded wheel SHA-256 or immutable
candidate build ID. Supply the chosen value independently to both
ReviewArtifact construction and loading. Never copy it from serialized artifact
metadata or repository prose, and never claim a Git SHA that was not
established.

## Required sequence

0. Drive `PreReviewSetup` through every phase, bind the confirmed resolved scope,
   and call `require_ready_to_review(current_scope=resolved_scope)` before
   canonical review. Cancellation produces no verdict. In headless use, any
   missing structured setup answer fails without calling or waiting on input.
1. Resolve and preview the scope as above, obtain explicit confirmation, then
   call `capture_resolved_scope(resolved)`, or capture the exact explicit
   headless scope with
   `capture_changeset(repository, base_ref, ScopeMode.PENDING)`. On a preflight
   error, no review exists. When `changeset.empty` is true, return
   `nothing_to_review` with process status 3; do not build semantic or review
   artifacts.
2. Call `discover_review_sources(repository, explicit_specs=...)`. Explicit
   requirements come from the invocation as `ProvidedRequirement` values.
   Pass a trusted source selection or additional evidence only when supplied by
   the caller under the contracts in docs 03 and 04.
3. Build the plan with:

   ```python
   plan = build_verification_plan(
       changeset,
       discovery,
       canonical_checks=discover_canonical_checks(repository),
       trusted_policy_checks=trusted_policy_checks,
       planner_additions=planner_additions,
   )
   ```

   The builder always adds the structural floor. Planner additions cannot
   replace protected checks.
4. Construct `ExecutionCapability` from the actual host adapter and trusted
   invocation policy. Set each non-waivable Boolean to true only when that
   invariant is enforced. Put only real host features in `available` (the plain
   local adapter provides bounded output; do not claim network, process, or
   resource isolation). Put a capability in `approval_waivable` only when
   trusted policy permits that risk, and in `approved_gaps` only after explicit
   human approval. Then call:

   ```python
   evidence = execute_verification_plan(
       changeset,
       discovery,
       plan,
       capability,
       timeout_seconds=timeout_seconds,
       output_limit_bytes=output_limit_bytes,
       required_capabilities=required_capabilities,
       redaction_values=explicit_secret_values,
   )
   ```

   Commands run only through this executor in disposable snapshots. Preserve
   `NOT_RUN`, failure kinds, required gaps, incomplete snapshots, and separate
   post-execution preservation failures exactly as recorded.
5. Inspect only change-relevant source and bounded evidence. Create every
   finding reference through `bind_semantic_reference(...)`, supplying the four
   identities from the current scope. Produce exactly one
   `SemanticAxisAssessment` for each frozen axis and any `SemanticFinding`,
   requirement comparison, or limit gap justified by the evidence. Semantic
   assessment is judgment, not a default: never mark axes PASS merely because a
   finding list is empty.
6. Bind and validate that judgment with:

   ```python
   assessment = build_semantic_assessment(
       changeset,
       discovery,
       plan,
       evidence,
       axes=axis_assessments,
       findings=findings,
       requirement_comparisons=requirement_comparisons,
       limit_gaps=limit_gaps,
   )
   assessment = load_semantic_assessment(
       assessment.model_dump(mode="json"),
       changeset,
       discovery,
       plan,
       evidence,
   )
   ```

   The loader rejects forged identities, references, missing winning-requirement
   comparisons, and invalid missing-requirement semantics.
   If complete semantic reconciliation over a non-empty established scope
   raises `SemanticLimitExceeded`, retain its `SemanticLimitGap`, mark every
   affected axis `INCONCLUSIVE` with `required_evidence_gap=True`, and build the
   structured review-level assessment with that gap. A real
   `requirement_comparisons...` collection gap may explain incomplete winning
   candidate coverage; no other gap may. Do not drop candidates, synthesize
   comparisons, or return preflight/code 3.
7. Give the reducer an externally known verifier version and build identifier;
   do not derive them from repository prose. Build and reload the final artifact:

   ```python
   artifact = build_review_artifact(
       changeset,
       discovery,
       plan,
       evidence,
       assessment,
       verifier_version=__version__,
       verifier_commit_or_build=verifier_build,
   )
   artifact = load_review_artifact(
       artifact.model_dump_json(),
       changeset,
       discovery,
       plan,
       evidence,
       assessment,
       verifier_version=__version__,
       verifier_commit_or_build=verifier_build,
   )
   ```
8. Record `artifact.verdict` and `verdict_exit_code(artifact.verdict)` before
   rendering. Render only with `render_markdown_report(artifact)`. A renderer
   failure is a reporting failure and must not replace or reinterpret the
   canonical verdict.

The canonical exit mapping is READY 0, NEEDS_CHANGES 1, INCONCLUSIVE 2, and
preflight/`nothing_to_review` 3. Keep the author repository unchanged throughout.
For compact executable fixtures covering all outcomes, see
`tests/test_v1_acceptance.py`.
