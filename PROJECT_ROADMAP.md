# Project Roadmap

## Current Pointer

- Current version: `0.1.7` released
- Current milestone: `1.7.8 / 0.1.8` — Semantic review depth + human-readable rationale
- Status: doing — implementation complete, pending real-project dogfood / independent review
- Next recommended step: real-project dogfood with a fresh reviewer, then independent review/release decision

## Milestone 1.7.8 / 0.1.8 — Semantic review depth + human-readable rationale

Status: doing — implementation complete, pending real-project dogfood /
independent review.

Goal: make PrePR Verify perform bounded senior-style semantic inspection rather
than treating green tests as sufficient, and expose concise per-axis review
rationale to humans.

Deliverables:

- strengthened Skill/runbook semantic inspection;
- ReviewArtifact `1.1.0` semantic summaries;
- human-readable `Semantic Review` report;
- focused regression tests.

Verification completed so far: focused deterministic tests, the full
deterministic suite, and schema/build/install checks.

Remaining gate: real-project LearnLoop dogfood and fresh independent review.

These tracks remain separate and unchanged:

- E baseline attribution;
- H environment readiness;
- 34-candidate requirement reconciliation capacity;
- F/G completed work.
