# Verification Strategy

## Planning boundary

Verification planning is hybrid:

```text
deterministic change signals
  -> LLM impact/risk proposal
  -> deterministic policy floor
  -> required/advisory verification plan
```

The LLM may add required or advisory checks but cannot remove a policy-required check. Every selected check records why it was selected. Risk affects planning, not the verdict reducer.

The universal floor covers complete scope capture, artifact/schema/invariant validation, preservation of the original working tree, and complete classification of selected-check outcomes. Test, lint, and build commands are not universally required across all repositories. A canonical check already established as mandatory must either complete reliably or produce a required evidence gap that prevents a `READY` verdict.

Canonical checks are discovered from repository-native tooling and documentation, with explicitly trusted company policy taking precedence when it requires a check. The LLM may then add targeted checks based on the change. If discovery finds no reliable canonical command, the report may recommend checks but does not invent a language-default matrix or install verification tooling.

The verification planner accepts bounded canonical declarations from `[tool.pre-pr-verify.verification]` in `pyproject.toml` and explicit trusted-policy check inputs. Each declaration uses structured argv, an optional repository-relative cwd, a required/advisory level, a selection reason, and an optional minimum environment profile. The only profiles are `FILESYSTEM_ONLY` and `GIT_REPOSITORY`. The configuration path and digest remain untrusted evidence bound to the executable snapshot; the declaration grants no execution permission. Absence of this explicit guidance produces no invented command.

Input channels fix origin and authority mechanically. Repository discovery emits only repository-canonical inputs, the planner API emits only model-proposed inputs, and trusted-policy checks use a separate provenance-bearing input with a trusted-policy label and digest. Callers cannot set `origin` on planner additions. IDs are unique across the complete plan; a planner collision with the deterministic floor, trusted policy, or repository canonical guidance is rejected rather than treated as replacement.

Environment-profile resolution is monotonic and separate from host execution
authority. Each command starts at `FILESYSTEM_ONLY`; a valid requirement from
the repository declaration, trusted policy, model proposal, or user invocation
may raise it to `GIT_REPOSITORY`, but no source may lower a stronger
requirement. The bounded raiser provenance is retained in canonical channel
order and participates in `VerificationPlan` identity. With no explicit
requirement, existing callers remain `FILESYSTEM_ONLY`. The planner does not
static-scan source, inspect command strings for hidden Git use, or classify
`pytest`, `make`, `npm`, or `cargo` as Git-aware by default.

Impact discovery is likewise generic: changed paths and content, repository search, practical references or call sites, adjacent code, existing tests/config/schema references, repository-native affected tooling, and semantic reasoning. V1 does not ship per-language AST or dependency-analysis frameworks. In a monorepo it prefers the repository's own affected/verify mechanism; if none establishes a reliable affected scope, that uncertainty remains evidence rather than a fabricated dependency graph.

## Execution contract

Command discovery is not execution permission. The core represents requested isolation, available host capability, approval state, structured argv/cwd/limits, and the execution result. It never equates `subprocess` with a security sandbox or claims a capability the host did not provide.

Repository code may execute only in a controlled disposable environment. Before execution, the host reports both non-waivable safety invariants and optional isolation capabilities. `FILESYSTEM_ONLY` uses the historical fresh disposable snapshot without `.git`. `GIT_REPOSITORY` uses a fresh independent standalone Git repository with its own Git administrative state, object database, `HEAD`, index, working tree, and sanitized configuration. It does not use linked worktrees, alternates/shared object stores, hardlink dependencies, or author Git authority. These profiles describe repository fidelity; they do not add or replace host `CapabilityName` or isolation capabilities.

Non-waivable invariants include structured execution without `shell=True` or shell interpolation, repository-bound paths, `.git/` protection, preservation of the author's working tree/index/HEAD, prevention of repository-controlled authority escalation, removal of secret-bearing host environment data, and enforcement of PrePR Verify's verdict invariants. Repository configuration and human approval cannot disable them. If a required non-waivable invariant cannot be met, the command must not run.

Some host capability gaps may be explicitly risk-accepted by a human, including unavailable network isolation, unavailable CPU/memory enforcement, or an unavailable sandbox feature, when policy classifies that capability as approval-waivable. Approval records awareness of the specific gap and permission to proceed; it does not make the command safe or claim the capability exists. An unapproved or non-waivable gap that blocks a required check contributes to `INCONCLUSIVE` after review scope exists.

Execution additionally uses time and output limits where supported. The complete security boundary is defined in `docs/04_security_and_trust.md`.

Execution status and failure kind are separate. Planned values include statuses such as passed, failed, not run, timed out, errored, and cancelled; failure kinds distinguish verification, infrastructure, permission, configuration, and unclassified failures. An unclassified required failure fails closed to `INCONCLUSIVE`.

V1 keeps the policy decision intentionally small: a command is safe and authorized to execute, requires explicit approval for a permitted capability gap, or cannot safely execute. It does not implement a general enterprise policy engine. Discovery never grants execution permission, and a required command that cannot run reliably leaves an evidence gap.

The `VerificationPlan` 1.1.0 contains deterministic change signals, three required structural invariants (scope capture, source preservation, and result classification), canonically ordered command checks, and an identity-bound environment profile plus bounded profile provenance for each command. The structural invariants are established by typed artifact validation and controlled planning/snapshot/execution flow; they deliberately have no `ExecutionResult`. A later reducer must not interpret that absence as missing command evidence. Model-proposed checks may add required or advisory coverage but cannot replace or downgrade deterministic-floor, trusted-policy, or repository-canonical checks.

`ExecutionRequest` binds structured argv, cwd, timeout, output bound, required capabilities, nonzero classification policy, snapshot identity, and any structured snapshot-materialization failure. Request construction also derives eligibility from the supplied SnapshotManifest, so an incomplete or failed manifest can only produce a non-executable request. `ExecutionResult` embeds that request and keeps process status separate from failure kind. Its validator deterministically re-derives the decision from the request and capability, requires non-executable decisions to be `not_run`, requires executed outcomes to be executable, and validates exit-code/failure-kind combinations. A nonzero result classified as `verification` is change-failure evidence only when the request carries explicit trustworthy attribution; successful process spawn alone does not prove that the intended verification workload ran or that its nonzero exit represents a candidate change failure. The generic request default is `unclassified`, so a required post-launch nonzero without reliable attribution remains an evidence gap and reduces Test Sufficiency to `INCONCLUSIVE`. Timeout and host-process errors are infrastructure failures; missing executable and missing cwd are configuration failures; permission and capability outcomes remain evidence gaps. Every non-executable decision also carries a structured blocked failure kind: approval requires permission, non-waivable capability gaps require capability, and cwd configuration/boundary failures require configuration/permission. If a valid ChangeSet exists but complete snapshot materialization is unavailable, execution emits a non-executable result bound to an explicitly incomplete manifest and a required evidence gap; it does not reclassify that later-stage failure as capture/preflight. Free-form reasons cannot change that binding. This verification stage emits classification evidence only and does not reduce `INCONCLUSIVE` or any readiness verdict.

Timeout is an absolute deadline for the whole process group and its captured stdout/stderr streams. Closed pipes do not prove that the group is finished: a parent that exits while a same-group descendant remains alive still consumes the request's remaining deadline. The group is terminated at the deadline and the result is `TIMED_OUT` plus `INFRASTRUCTURE`. Any exception after `Popen` owns bounded group cleanup and pipe closure before returning. Cwd validation, including missing paths, escapes, symlink loops, `.git`, and unsafe relative components, is classified as `NOT_RUN` evidence and never starts the process.

The direct-Git boundary is deliberately smaller than a Git option parser. In a
resolved `GIT_REPOSITORY` check, only structured argv whose executable basename
is `git` is classified. The bounded supported forms are `rev-parse HEAD`,
`rev-parse --show-toplevel`, `ls-files` with simple pathspecs and literal `--`,
`status`, `status --porcelain`, `status --porcelain=v1`, `status --short`,
`diff`, and `diff --cached`, with simple `--` pathspec forms for the diffs.
Other direct Git requests, including history, tags, remotes, reflogs,
submodules, worktrees, LFS, and repository/config redirects, are rejected
before execution as structured capability or configuration evidence gaps.
Indirect commands remain opaque: nested Git use is not statically scanned or
reclassified from stderr. A successfully spawned indirect command is a
verification failure only when its request carries explicit trustworthy
`VERIFICATION` attribution; otherwise a nonzero result remains
`UNCLASSIFIED`, and a required check contributes an evidence gap.

Git materialization owns fixed core limits of 100,000 tracked entries, 250,000
imported objects, 1 GiB logical object bytes, and 1 GiB materialized bytes.
Repository, model, and user inputs cannot raise these limits. An over-limit or
otherwise incomplete materialization is never executable and has no fallback.

Hardened source Git reads use `GIT_OPTIONAL_LOCKS=0`. PrePR Verify performs no
intentional source Git writes and validates source `HEAD`, index, working tree,
and configuration preservation fail-closed. This protects Git authority and
source linkage; it does not claim that arbitrary repository code cannot open
unrelated host paths when the host provides no OS-level filesystem sandbox.

Existing repository security scanners may be discovered and integrated like other canonical checks. Without one, semantic review may still identify security risk. V1 neither installs scanners such as Semgrep, Bandit, or Snyk nor makes a named scanner universally mandatory.

## Evidence and observability

For every selected check, record its requirement level, selection reason, argv, duration, result classification, skips, and evidence contribution. Persist only redacted bounded excerpts, output digests, and optional protected ephemeral raw-log references. Do not persist the default environment or raw logs in the repository. Redaction is best-effort, not a guarantee that arbitrary secret formats will be recognized.

The versioned `VerificationEvidence` 1.1.0 contract binds the plan and one command-execution record for every planned command. Each record contains its own freshly materialized pristine snapshot manifest (or an explicitly incomplete manifest when materialization leaves a required evidence gap), deterministic materialization ordinal, identity-bound environment profile, request, capability, revalidated decision, and result. If source preservation fails after a child has executed, the actual result and bounded output remain in that record and a separate bound `source_preservation_failures` signal records the required evidence gap; the execution is never rewritten as `not_run`. Commands never share a writable snapshot. A repository-native build-then-test sequence that intentionally shares generated state must be expressed as one canonical command; V1 does not add a command dependency graph. The evidence contract has an independent schema lifecycle and contains no semantic assessment, axis, finding, or readiness reducer. VerificationPlan/Evidence 1.0.0 remain frozen legacy contracts and are loaded without inserting profile fields or recomputing identity under 1.1.0 rules.

Semantic review may inspect the captured ChangeSet's complete effective UTF-8 text progressively with generic search/context to support impact and test-sufficiency reasoning. Persisted context excerpts remain bounded previews and are not a source-reading or model-token cap. It does not execute commands, reread an unbound repository moment, install scanners, or infer a language/dependency matrix. Any proposed targeted verification remains a semantic finding or separate proposal; execution remains solely the deterministic adapter. Semantic findings bind to the existing ChangeSet, DiscoveryResult, VerificationPlan, and VerificationEvidence identities.

ReviewArtifact construction consumes this evidence without executing commands. Structural
floor checks are summarized as satisfied by their validated artifact/control
flow; their intentional lack of `ExecutionResult` is not a gap. Command status
and failure kind remain distinct in the ReviewArtifact summary: `not_run` is
never rewritten as an executed failure, and failed required verification is
distinguished from timeout, capability, configuration, permission,
infrastructure, and unclassified gaps. Incomplete snapshots therefore cannot
appear successful. Post-execution source-preservation failures remain separate
from the retained command result and invalidate readiness confidence.

## Evaluation

Default CI is deterministic, no-network, and uses real temporary Git repositories where Git behavior matters. Unit, integration, security, schema, invariant, and fixture tests must all pass.

Opt-in model evaluation uses representative repositories and a fixed rubric for axis classification, finding detection, grounding, and verdicts. Safety-critical cases are hard gates: READY, regression, spec mismatch, unavailable required verification, missing required evidence, and unsupported suspicion must all produce the required safe behavior. Missing-test quality is scored separately and cannot average away a verdict-safety failure.
