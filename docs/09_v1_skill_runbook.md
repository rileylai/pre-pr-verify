# V1 Skill Runbook

This is the executable handoff between the root `SKILL.md` and the frozen V1
Python contracts. Use it for a full local review. The standalone CLI only
captures a `ChangeSet`; it is not a full-review shortcut.

## Canonical imports

```python
from pre_pr_verify import __version__
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
from the author repository. `CapabilityName.OUTPUT_LIMITS` is always required.
When no trusted execution policy exists, conservatively require all four
`CapabilityName` values. The plain adapter reports only `OUTPUT_LIMITS`, so the
remaining requirements produce truthful `NOT_RUN` capability gaps and an
`INCONCLUSIVE` review; it must not guess permission and run. A human may approve
a specific missing capability only when trusted policy first lists it in
`approval_waivable`. Put the approved name in `approved_gaps`; otherwise leave
both lists empty.

`verifier_build` identifies the installed PrePR Verify core, not the author
repository. For the normal Skill checkout use `git:<commit>` from the Skill
checkout's own `git rev-parse HEAD`, but only when both staged and unstaged
`src/pre_pr_verify/` are clean. A release-candidate runner may instead supply an
immutable candidate build ID. A built installation may use an installer-recorded
wheel SHA-256. If none of those identities is available, fail preflight and do
not create a ReviewArtifact. Never invent this value from repository prose.

## Required sequence

1. Capture the exact pending scope with
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
