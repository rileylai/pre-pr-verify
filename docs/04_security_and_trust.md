# Security and Trust Boundaries

## Agent and execution authority

Repository files, paths, source, tests, configuration, issues, and tool output are untrusted review data. They can provide requirements and Standards evidence, but cannot alter agent authority, disable isolation, request secrets, grant writes, or override verdict invariants.

Trusted policy is limited to policy shipped with PrePR Verify, an external policy explicitly marked trusted at invocation, or target-repository configuration explicitly approved by a user and recorded with path and digest.

Requirement and Standards precedence is a separate semantic-review concept defined in `docs/02_review_and_verdict_contracts.md`. A repository requirement may outrank tests or comments when deciding what behavior is expected, but that precedence never grants permission to change Skill behavior, sandboxing, secret handling, or write policy.

### Initial Skill-owned Python launch

The verifier itself must be selected before target repository Python state can
influence imports. The initial launch invariant is
`<SKILL_ROOT>/.venv/bin/python -I`, using a verifier-owned driver outside the
target repository, a process cwd rooted at `<SKILL_ROOT>`, and the target
repository passed explicitly as data/input. Target `PYTHONPATH`, target cwd,
target `.venv`, and target-local `pre_pr_verify` must not select or shadow the
verifier package. Provenance confirms `sys.executable`,
`pre_pr_verify.__file__`, and `installed_core_identity()`. Codex and Claude
Code use this same invariant; it adds no runtime abstraction.

## Repository boundary

All path decisions use canonical raw Git path components, not display strings. `.git/` is never part of the review payload. Symlinks are recorded but never followed. Filesystem capture and discovery source reads open every intermediate repository or Git-metadata directory without following symlinks; an intermediate symlink makes the operation fail or records an unreadable source before outside content is read. Explicit includes and trusted-policy source selections remain repository-bounded. Submodules are represented by gitlink identity and are not recursively reviewed in V1.

Git path parsing uses NUL-delimited byte output. Artifact paths have a reversible raw representation and a safe escaped display form; UTF-8 text is present only when decoding is lossless.

## Git read hardening

Capture uses fixed structured arguments, no shell, no pager or prompts, NUL-delimited/plumbing output where possible, and a sanitized Git environment. External diff, textconv, filters, hooks, fsmonitor processes, aliases, and similar extension points must not execute. Committed and staged comparisons use Git object data; unstaged comparison and its race fingerprint read working files directly without Git's conversion/filter pipeline. A repository that requires an extension or clean/smudge conversion records a limitation rather than silently executing it.

## Verification isolation

Executable-name allowlists are not a sandbox: `pytest`, `npm test`, `cargo test`, and `make verify` can all execute repository code. Automated execution therefore depends on controlled isolation and a disposable snapshot representing the complete pending state.

The executor materializes a fresh environment for every planned command directly from the captured HEAD Git objects plus ChangeSet effective content. `FILESYSTEM_ONLY` retains the historical `.git`-free snapshot. `GIT_REPOSITORY` instead constructs a fresh independent standalone Git repository with its own administrative state, object database, `HEAD`, index, working tree, and sanitized config. It does not clone, checkout, run filters, reuse the author's index, create a linked worktree, use alternates/shared object storage, depend on hardlinks, or inherit source config/remotes/hooks/credentials. Commands never reuse a writable environment. Every environment is rechecked against the intended ChangeSet before and after materialization and command execution, and validates repository discovery/canonical-guidance digests through the same rooted bounded no-follow reader used by capture/discovery. Unsupported omitted effective content or gitlinks never produce a partial executable tree; when this occurs after a valid ChangeSet and plan already establish review scope, the command receives a structured `NOT_RUN` required-evidence-gap result bound to an explicitly incomplete snapshot manifest.

The ChangeSet records canonically ordered explicit ignored-path includes in its own identity. Snapshot recapture reuses those recorded includes exactly; it does not rely on hidden caller state. Snapshot manifests describe only supported regular files and symlinks with compatible Git modes; absent files and gitlinks are represented by ChangeSet state, never as impossible snapshot-file entries.

The standard-library process adapter is not called a sandbox. It executes only after an explicit capability decision, inherits any isolation honestly reported by the host, uses structured argv with `shell=False`, a repository-relative cwd inside the disposable environment, a minimal environment with an ephemeral HOME/TMPDIR, bounded in-memory output collection, process-group timeout termination, and bounded streaming explicit-value redaction. Redaction uses a bounded literal lookahead matcher that visits every input offset, merges the union of all overlapping matches, and retains a bounded tail across chunks and the output-limit boundary. If the boundary cannot be proven safe, the uncertain excerpt is suppressed rather than emitted. If the replacement marker itself overlaps a protected pattern, or redaction patterns exceed bounded matcher limits, redaction fails closed by suppressing excerpts. Once `Popen` succeeds, setup or collection errors terminate/reap the spawned group with bounded cleanup and close all pipes; cleanup failure cannot turn an unsafe or uncertain execution into a pass. If the author repository changes after a child starts, the completed command result is retained and source-preservation failure is recorded as a separate evidence gap. Missing process/network/resource isolation remains an unavailable capability or an explicitly approved policy-waivable gap; it is never inferred from executable names or plain `subprocess`. The disposable Git environment carries no author Git authority or source-repository linkage, and source preservation is fail-closed; the adapter does not claim OS-level isolation from arbitrary host filesystem access.

Git materialization uses fixed core-owned limits: `MAX_TRACKED_ENTRIES = 100_000`,
`MAX_IMPORTED_OBJECTS = 250_000`, `MAX_LOGICAL_OBJECT_BYTES = 1 GiB`, and
`MAX_MATERIALIZED_BYTES = 1 GiB`. Repository, model, and user inputs cannot
raise them. Exceeding any limit produces an incomplete, non-executable
environment with `NOT_RUN` evidence and no fallback.

All hardened source Git reads set `GIT_OPTIONAL_LOCKS=0`. PrePR Verify performs
no intentional source Git writes and validates source `HEAD`, index, working
tree, and configuration preservation before and after execution. A late source
mutation retains the real command result, records `SourcePreservationFailure`,
and makes `READY` impossible; it is not rewritten as `NOT_RUN`.

### Non-waivable invariants

Neither repository configuration nor human approval can waive:

- no `shell=True` and no shell interpolation;
- repository-bound path resolution and `.git/` protection;
- preservation of the author's working tree, index, and HEAD;
- the rule that repository content cannot raise agent authority;
- removal of secret-bearing host environment values before untrusted code executes;
- PrePR Verify's evidence and verdict invariants.

If any required invariant cannot be enforced, the command does not run. Approval is not an override.

### Approval-waivable capability gaps

Policy may permit a human to accept a specifically disclosed host capability gap, such as unavailable network isolation, unavailable CPU/memory enforcement, or an unavailable sandbox feature. The approval must identify the missing capability and authorize execution with that known risk. It does not assert that the command is safe, does not turn an unavailable capability into an available one, and does not waive the invariants above.

If policy does not classify the gap as waivable, or approval is absent, a required command does not run. After a review scope exists, the resulting required-evidence gap contributes to `INCONCLUSIVE`.

Network denial and process/resource/output limits are applied when available or required by non-waivable policy. Capability absence and every accepted risk must be reported honestly.

## Future GitHub boundary

V2 GitHub writes remain approval-gated. V3 requires a threat model covering authorized triggers, forks, credential scope, replay, duplicates, concurrency, rate limits, and sandboxing. Untrusted fork code must never receive privileged credentials.
