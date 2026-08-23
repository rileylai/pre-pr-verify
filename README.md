# PrePR Verify

PrePR Verify is an independent, evidence-based pre-PR verification Skill for AI-assisted software development. A fresh reviewer reconstructs context from repository state instead of trusting the implementation session's claim that the work is complete.

The product is a Codex Skill backed by a small deterministic Python core. The Skill owns semantic judgment and workflow orchestration. The core owns Git scope, typed artifacts, command contracts, evidence references, schema validation, and verdict invariants.

## Review model

Every full review keeps three results separate:

- **Spec:** Did we build the right thing?
- **Standards:** Did we build it the right way for this repository?
- **Verification:** Can we prove it works?

Each axis is `PASS`, `FAIL`, or `INCONCLUSIVE`. The final verdict is `READY`, `NEEDS_CHANGES`, or `INCONCLUSIVE`. Missing required evidence never produces `READY`.

## Version boundaries

- **V1:** local independent review and deterministic verification core.
- **V2:** user-initiated GitHub PR review through GitHub MCP, with approval-gated top-level publication.
- **V3:** authorized event trigger and deterministic inline mapping. MCP remains an integration interface, not the trigger.

Only V1 is open for implementation. The current milestone is recorded in `dev_state/PROJECT_ROADMAP.md`.

Milestone 1.6 is the first point at which the complete local review workflow exists and can be dogfooded as a release candidate. Milestone 1.7 adds the acceptance, security, model-evaluation, and self-hosting evidence required before V1 can serve as a trusted gate for later V2/V3 work.

## Development

The package supports Python 3.11 or newer. Development is pinned to Python 3.12 and uses uv.

```sh
uv sync --dev
uv run pytest
```

Milestone 1.2 provides deterministic ChangeSet capture only:

```sh
uv run pre-pr-verify capture \
  --repo /path/to/repository \
  --base main \
  --scope pending
```

The command writes canonical ChangeSet JSON to stdout unless `--output` is explicitly supplied. It performs no code review and emits no readiness verdict. The Python library also provides bounded requirement and Standards source discovery; execution, semantic reasoning, ReviewArtifact, and GitHub integration remain unimplemented.

## Documentation

Start with `AGENTS.md`, then follow its task-specific documentation map. Design contracts live in numbered files under `docs/`; resumable engineering state lives under `dev_state/`.
