# Development Validation and Self-Hosting

## Purpose

PrePR Verify should validate its own development as capabilities become real, without claiming unfinished components already form a trustworthy readiness gate. Dogfooding supplements milestone evidence; it does not erase bootstrap trust requirements.

## Progressive dogfooding

### Milestone 1.2: ChangeSet capture

Dogfood `capture` against the PrePR Verify repository's committed, staged, unstaged, and non-ignored untracked state. Check identity stability, origin layering, path handling, empty-state behavior, and preservation of the source repository. This exercises repository capture only and is not code review.

### Milestone 1.3: requirement and Standards discovery

Dogfood spec discovery, Standards discovery, source precedence, conflicts, and the distinction between requirement precedence and agent authority. This still does not constitute a complete V1 review.

### Milestone 1.4: verification planning and execution

Dogfood deterministic change signals, required/advisory planning, command execution against a disposable snapshot, and result/evidence classification. PrePR Verify may run its own deterministic development checks at this stage, but the semantic and verdict pipeline is incomplete.

### Milestone 1.5: semantic review

Dogfood semantic Spec, Standards, missing-test, and contextual-security reasoning. Findings must be grounded using the contracts implemented so far. Without the final evidence validator and reducer, this is not a mechanically validated V1 readiness gate.

### Milestone 1.6: complete V1 release-candidate review

Milestone 1.6 is the first point at which the entire local pipeline exists:

```text
ChangeSet
  -> discovery
  -> planning and execution
  -> semantic review
  -> evidence
  -> deterministic axis reducer
  -> final verdict
  -> canonical ReviewArtifact
  -> Markdown report
```

After 1.6, a fresh Codex session can run a genuine local V1 dogfood review of the candidate. The result is release-candidate evidence, not yet a trusted release gate.

### Milestone 1.7: trusted V1 gate readiness

Milestone 1.7 adds the deterministic acceptance suite, security fixtures, READY/NEEDS_CHANGES/INCONCLUSIVE fixtures, model-evaluation verdict-safety hard gates, and self-hosting validation. Only after these gates pass and V1 is released may a last-known-good V1 verifier serve as the required pre-PR gate for V2/V3 development.

For the V1 release candidate, model-evaluation evidence is a fresh independent
Codex Skill forward-test against realistic local-review requests and the fixed
verdict-safety acceptance rubric. It is not a provider runtime, API-key-based
test harness, token counter, or orchestration framework. Deterministic fixtures
remain the reproducible hard gates; the independent Skill review supplies the
separate semantic/bootstrap evidence.

Release acceptance also requires a locked clean sync, sdist/wheel build,
installation and import from the built wheel in a disposable environment,
checked-in schema availability, root Skill validation, documentation/limitation
consistency, full deterministic tests, and self-dogfood over the complete pending
ChangeSet. The repository checkout is the Skill distribution; built Python
artifacts are the deterministic core distribution.

The post-V1 setup usability gate also includes a fresh Skill forward-test in
which a narrow LearnLoop-shaped review is completed with numeric scope,
requirement, and verification choices plus one explicit final confirmation.
The test must prove that source acknowledgement does not alter requirement
precedence, repository commands do not grant execution authority, and headless
setup never prompts or invents missing inputs.

## Bootstrap and circular trust

An unreleased candidate cannot establish its own trust merely by reviewing itself and returning `READY`. Before the first V1 release, candidate self-review is additional dogfood evidence only.

Bootstrap release evidence requires all of:

- deterministic unit, integration, and security tests;
- acceptance fixtures, including verdict-safety cases;
- fresh-context external or manual review independent of the implementation session;
- the candidate's dogfood result, with limitations retained.

No single item substitutes for the others, and candidate self-review is never the sole release evidence.

## Last-known-good verifier

After V1 release, development should use a previously trusted release to review the next candidate:

```text
last-known-good PrePR Verify
  -> review next candidate version
```

For example, a released v1.0.0 verifier reviews the v1.1 working state. Running the candidate against itself may provide comparison evidence but cannot be the only trusted gate.

The ReviewArtifact contract records explicit fields for:

- verifier version;
- verifier commit or build identity;
- target ChangeSet or snapshot identity.

These fields are validated release evidence, not self-asserted serialized
provenance.

## Development stage gates

Before milestone 1.6:

```text
Implement one approved milestone
  -> deterministic milestone tests
  -> dogfood only the capability currently available
  -> fresh-session review
  -> documentation and dev-state update
```

After milestone 1.6 and before V1 release:

```text
Implementation Session A
  -> implement one approved change
  -> deterministic development tests
  -> Fresh Session B
  -> PrePR Verify V1 local release-candidate review
  -> READY / NEEDS_CHANGES / INCONCLUSIVE
```

After milestone 1.7 and V1 release:

```text
Future V2/V3 milestone
  -> normal milestone verification
  -> last-known-good V1 PrePR Verify
  -> READY required before PR or merge
```

Preflight failure or `nothing_to_review` exits with code `3`, produces no readiness verdict, and therefore cannot satisfy a stage that requires `READY`.
