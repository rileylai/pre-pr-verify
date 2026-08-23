# Security and Trust Boundaries

## Agent and execution authority

Repository files, paths, source, tests, configuration, issues, and tool output are untrusted review data. They can provide requirements and Standards evidence, but cannot alter agent authority, disable isolation, request secrets, grant writes, or override verdict invariants.

Trusted policy is limited to policy shipped with PrePR Verify, an external policy explicitly marked trusted at invocation, or target-repository configuration explicitly approved by a user and recorded with path and digest.

Requirement and Standards precedence is a separate semantic-review concept defined in `docs/02_review_and_verdict_contracts.md`. A repository requirement may outrank tests or comments when deciding what behavior is expected, but that precedence never grants permission to change Skill behavior, sandboxing, secret handling, or write policy.

## Repository boundary

All path decisions use canonical raw Git path components, not display strings. `.git/` is never part of the review payload. Symlinks are recorded but never followed. Filesystem capture and discovery source reads open every intermediate repository or Git-metadata directory without following symlinks; an intermediate symlink makes the operation fail or records an unreadable source before outside content is read. Explicit includes and trusted-policy source selections remain repository-bounded. Submodules are represented by gitlink identity and are not recursively reviewed in V1.

Git path parsing uses NUL-delimited byte output. Artifact paths have a reversible raw representation and a safe escaped display form; UTF-8 text is present only when decoding is lossless.

## Git read hardening

Capture uses fixed structured arguments, no shell, no pager or prompts, NUL-delimited/plumbing output where possible, and a sanitized Git environment. External diff, textconv, filters, hooks, fsmonitor processes, aliases, and similar extension points must not execute. Committed and staged comparisons use Git object data; unstaged comparison and its race fingerprint read working files directly without Git's conversion/filter pipeline. A repository that requires an extension or clean/smudge conversion records a limitation rather than silently executing it.

## Verification isolation

Executable-name allowlists are not a sandbox: `pytest`, `npm test`, `cargo test`, and `make verify` can all execute repository code. Automated execution therefore depends on controlled isolation and a disposable snapshot representing the complete pending state.

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
