# Changelog

## 0.1.1 - 2026-08-24

- Reject ambiguous custom ref names and non-commit ref targets while preserving
  successful SHA pinning.
- Keep pre-selection discovery and preview metadata-only; canonical content
  capture starts after explicit confirmation.
- Recommend working changes for a materially narrower dirty scope when the
  plausible branch scope exceeds existing large-scope thresholds.
- Add a human-attached Scope Intent Resolver for working changes, current
  branch with explicit base selection, inclusive recent feature-start commits,
  and custom refs.
- Add deterministic pre-review scope previews and advisory-only large or mixed
  scope confirmation choices.
- Fail closed when headless invocation lacks complete explicit scope, and keep
  review focus separate from the canonical readiness boundary.
- Preserve the V1 ChangeSet, SemanticAssessment, ReviewArtifact, reducer,
  report, and exit contracts unchanged.

## 0.1.0 - 2026-08-24

- Initial local V1 release candidate with deterministic ChangeSet capture,
  discovery, isolated verification evidence, five-axis semantic assessment,
  ReviewArtifact reduction, and Markdown reporting.
