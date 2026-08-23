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

The universal floor covers complete scope capture, artifact/schema/invariant validation, preservation of the original working tree, and complete classification of selected-check outcomes. Test, lint, and build commands are not universally required across all repositories. A canonical check already established as mandatory must either complete reliably or make Verification `INCONCLUSIVE`.

## Execution contract

Command discovery is not execution permission. The core represents requested isolation, available host capability, approval state, structured argv/cwd/limits, and the execution result. It never equates `subprocess` with a security sandbox or claims a capability the host did not provide.

Repository code may execute only in a controlled disposable snapshot. Before execution, the host reports both non-waivable safety invariants and optional isolation capabilities.

Non-waivable invariants include structured execution without `shell=True` or shell interpolation, repository-bound paths, `.git/` protection, preservation of the author's working tree/index/HEAD, prevention of repository-controlled authority escalation, removal of secret-bearing host environment data, and enforcement of PrePR Verify's verdict invariants. Repository configuration and human approval cannot disable them. If a required non-waivable invariant cannot be met, the command must not run.

Some host capability gaps may be explicitly risk-accepted by a human, including unavailable network isolation, unavailable CPU/memory enforcement, or an unavailable sandbox feature, when policy classifies that capability as approval-waivable. Approval records awareness of the specific gap and permission to proceed; it does not make the command safe or claim the capability exists. An unapproved or non-waivable gap that blocks a required check contributes to `INCONCLUSIVE` after review scope exists.

Execution additionally uses time and output limits where supported. The complete security boundary is defined in `docs/04_security_and_trust.md`.

Execution status and failure kind are separate. Planned values include statuses such as passed, failed, not run, timed out, errored, and cancelled; failure kinds distinguish verification, infrastructure, permission, configuration, and unclassified failures. An unclassified required failure fails closed to `INCONCLUSIVE`.

## Evidence and observability

For every selected check, record its requirement level, selection reason, argv, duration, result classification, skips, and evidence contribution. Persist only redacted bounded excerpts, output digests, and optional protected ephemeral raw-log references. Do not persist the default environment or raw logs in the repository. Redaction is best-effort, not a guarantee that arbitrary secret formats will be recognized.

## Evaluation

Default CI is deterministic, no-network, and uses real temporary Git repositories where Git behavior matters. Unit, integration, security, schema, invariant, and fixture tests must all pass.

Opt-in model evaluation uses representative repositories and a fixed rubric for axis classification, finding detection, grounding, and verdicts. Safety-critical cases are hard gates: READY, regression, spec mismatch, unavailable required verification, missing required evidence, and unsupported suspicion must all produce the required safe behavior. Missing-test quality is scored separately and cannot average away a verdict-safety failure.
