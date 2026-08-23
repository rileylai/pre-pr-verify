# Repository Scope and ChangeSet Contract

## Explicit comparison

V1 requires an explicit repository and base ref. It does not infer a default branch. Capture records the requested base ref, resolved base commit, merge-base commit, HEAD commit, and scope mode. Invalid invocation, a non-Git repository, a missing or invalid ref, and an absent merge base/comparison scope are preflight failures. They return exit code `3`; because no review exists, they do not produce a readiness verdict.

The default `pending` scope represents:

- committed branch changes from merge base through HEAD;
- staged changes from HEAD to index;
- unstaged changes from index to working tree;
- non-ignored untracked files.

`committed-only` excludes staged, unstaged, and untracked changes. Ignored files are excluded unless explicitly included by the user, and `.git/` is always excluded.

The frozen milestone 1.2 CLI does not add an include flag. An embedding caller may pass explicitly approved ignored paths through the core capture API; these paths remain byte-validated, repository-bounded, and unable to enter `.git/`. `committed-only` rejects explicit includes.

## Layered state

A logical file change preserves base, HEAD, index, working, and effective states plus every applicable origin. A path may therefore have multiple origins. Committed and staged rename detection uses fixed Git object comparison (`-M50%`, Myers algorithm, bounded candidate set). Working-tree rename detection is limited to an unambiguous exact-content match and never invokes conversion filters. Copies are not inferred in milestone 1.2.

File state includes path, presence, kind, executable/mode information, size, and content identity. Regular and binary content uses SHA-256 of exact bytes; symlinks hash link-target bytes without following them; gitlinks use commit identity; deletion uses an explicit absence marker.

The whole ChangeSet identity hashes a canonical semantic payload. Timestamps, durations, temporary paths, and other operational metadata are excluded.

## Content capture

Default capture limits are 1 MiB per file and 10 MiB total. Files are considered in deterministic order so the same state yields the same captured/omitted set. Omitted files retain metadata, digest, and omission reason. Omission becomes inconclusive only when the missing content is required evidence.

Symlinks are never followed, including symlinks in intermediate directory components. Working-tree, explicit-include, and directly read Git-metadata paths are traversed relative to a fixed root with no-follow directory opens. Submodules are not traversed, and binary files remain byte-identified. Path parsing is byte-safe and NUL-delimited.

## Consistency

Capture compares HEAD, index identity, a deterministic porcelain-equivalent status/path fingerprint, and captured/effective working-content identity before and after collection. The status fingerprint is derived from fixed plumbing and direct filesystem state rather than invoking extension-capable working-tree diff/status conversion. A changed state discards the entire attempt and retries once. A second unstable attempt produces a capture failure; data from different moments is never blended.

Capture instability is a capture/preflight failure when it prevents creation of a reliable ChangeSet. It therefore produces no readiness verdict. A later-stage inability to obtain required evidence from an already established, non-empty ChangeSet is instead a review-level `INCONCLUSIVE` result.

## Empty effective state

An empty effective diff is valid deterministic capture output. The ChangeSet sets `empty = true`, still computes its identity, and `capture` exits `0`.

A full pre-PR review cannot treat absence of change as evidence that Spec, Standards, and Verification passed. It stops before axis assessment with `nothing_to_review`, exit code `3`, and no readiness verdict. The ChangeSet remains valid; the full-review scope is simply not reviewable.

## ChangeSet schema lifecycle

ChangeSet has its own versioned schema beginning in milestone 1.2. Python typed models and code invariants are its executable source of truth; its deterministic generated JSON Schema is checked in. Its version lifecycle is independent of the ReviewArtifact schema introduced in milestone 1.6. A field named `schema_version` always versions the enclosing contract, never the entire project.

## Milestone 1.2 executable contract

The CLI surface is:

```text
pre-pr-verify capture --repo <path> --base <ref> \
  --scope pending|committed-only [--output <path>]
```

It captures and validates a deterministic ChangeSet only. Output defaults to stdout. File output requires an explicit path and may not target `.git/`. Successful capture, including an empty ChangeSet, exits `0`; invalid invocation/scope exits `3`; an internal contract/tool error exits `4`. This milestone does not produce a readiness verdict, materialize an executable snapshot, or run verification commands.

Its hard test matrix includes real temporary repositories covering committed divergence and merge-base behavior; every pending layer, including tracked unstaged-only state; combined origins; deletion, rename, mode, symlink, gitlink, binary, large and ignored files; tab/newline hostile filenames; intermediate-symlink escape; invalid and empty scopes; HEAD/index/status/content capture races; deterministic identity; Git extension suppression; committed-only exclusion; and complete preservation of the source repository. Platform filesystems that cannot create non-UTF-8 names use an explicit skip plus byte-safe model coverage.

The ChangeSet schema is generated from the typed model and checked in as `schemas/changeset-1.0.0.schema.json`. Schema drift is a deterministic test failure.
