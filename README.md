# PrePR Verify

PrePR Verify is an independent, evidence-based pre-PR verification Skill for AI-assisted software development. A fresh reviewer reconstructs context from repository state instead of trusting the implementation session's claim that the work is complete.

The product is a Codex Skill backed by a small deterministic Python core. The Skill owns semantic judgment and workflow orchestration. The core owns Git scope, typed artifacts, command contracts, evidence references, schema validation, and verdict invariants.

## Review model

Every full review keeps three concerns separate:

- **Spec:** Did we build the right thing?
- **Standards:** Did we build it the right way for this repository?
- **Verification:** Can deterministic evidence prove it works?

The canonical semantic and reduced axes are **Spec**, **Standards**, **Impact**,
**Test Sufficiency**, and **Contextual Security**. Each is `PASS`, `FAIL`, or
`INCONCLUSIVE`. Verification remains separate evidence; its outcomes and gaps
propagate into the axes and final verdict. The final verdict is `READY`,
`NEEDS_CHANGES`, or `INCONCLUSIVE`. Missing required evidence never produces
`READY`.

## Version boundaries

- **V1:** local independent review and deterministic verification core.
- **V2:** user-initiated GitHub PR review through GitHub MCP, with approval-gated top-level publication.
- **V3:** authorized event trigger and deterministic inline mapping. MCP remains an integration interface, not the trigger.

V1 is the implemented local product. V2/V3 remain unimplemented. Local milestone
development may keep ignored resumable state under `dev_state/`; installed Skill
use does not depend on it.

Milestone 1.7 validates the complete local workflow, installability, Skill
instructions, acceptance scenarios, and self-hosting evidence before a final
fresh release review.

## Install and use the Skill

Install the repository as a Codex Skill checkout, then install its locked Python
core:

```sh
git clone https://github.com/rileylai/pre-pr-verify.git \
  ~/.codex/skills/pre-pr-verify
cd ~/.codex/skills/pre-pr-verify
uv sync --locked
```

In a human-attached Codex session, invoke it with a repository and select one of
the displayed scope intents and boundaries, for example:

```text
Use $pre-pr-verify to review /path/to/repository. I want current branch scope;
show me the bounded base candidates before I choose one.
Treat docs/feature-spec.md as the explicit requirement.
```

The interactive setup supports working changes, current branch with explicit
base selection, an inclusive feature-start commit selected from recent history,
and a custom base/ref. It previews the resolved boundary and size before
semantic review using Git metadata only; full source capture begins after
confirmation. Ambiguous custom short refs fail preflight, and unavailable
content-free line estimates are shown as unavailable. Recommendations and
large-scope warnings are advisory: the
Skill never silently selects working changes, `main`, or any other boundary.
Automation must provide the repository and complete explicit scope/config; if
anything is missing it fails preflight without prompting or waiting.

The root `SKILL.md` is the full-review entrypoint. It orchestrates the canonical
Python builders/loaders for ChangeSet, discovery, planning/execution,
SemanticAssessment, ReviewArtifact, report, and exit semantics. The standalone
CLI intentionally exposes only deterministic capture; it is not a prompt-free
semantic-review command.
The exact full-review imports and call sequence are in
[`docs/09_v1_skill_runbook.md`](docs/09_v1_skill_runbook.md).

## Development

The package supports Python 3.11 or newer. Reproducible development is pinned to
uv-managed CPython 3.12.13 through `.python-version`.

```sh
uv sync --locked --dev
uv run pytest
uv build
```

To validate the built core independently of the source checkout:

```sh
uv venv /tmp/pre-pr-verify-install
uv pip install --python /tmp/pre-pr-verify-install/bin/python \
  dist/pre_pr_verify-0.1.1-py3-none-any.whl
/tmp/pre-pr-verify-install/bin/python -c \
  "import pre_pr_verify; print(pre_pr_verify.__version__)"
```

The wheel/sdist are the Python core distribution. The Git checkout is the Codex
Skill distribution and therefore contains `SKILL.md`, numbered references, and
checked-in schemas.

The deterministic capture CLI is:

```sh
uv run pre-pr-verify capture \
  --repo /path/to/repository \
  --base main \
  --scope pending
```

The command writes canonical ChangeSet JSON to stdout unless `--output` is
explicitly supplied. It performs no semantic review and emits no readiness
verdict. The installed Python core provides every V1 contract and canonical
builder/loader; checked-in schemas live under `schemas/`, and installed code can
render the same schemas through `pre_pr_verify.schema`.

No `.env`, Codex/Claude/OpenAI API key, provider-specific token management, or
network service is required by the deterministic core or default test suite.

## Intentional V1 limits

- Local macOS/Linux Git repositories only; Windows is deferred.
- Explicit-value output redaction is bounded and best-effort.
- No provider/model token or context-window policy.
- No language-specific AST/dependency engine.
- No scanner auto-installation.
- No inferred monorepo dependency engine or enterprise policy engine.
- No GitHub MCP, PR publication, event trigger, V2, or V3 behavior.

## Documentation

Start with `AGENTS.md`, then follow its task-specific documentation map. Design contracts live in numbered files under `docs/`; resumable engineering state lives under `dev_state/`.
