# Versioned Integrations

## V1: local core

V1 reconstructs local repository context, performs the five-axis review, runs relevant deterministic verification under explicit capability constraints, produces structured evidence, and reduces a fail-closed verdict. It has no GitHub dependency.

## V2: GitHub MCP

V2 maps PR title, description, base/head, commits, changed files, linked requirements, checks, and useful existing review context into the same internal review core. It does not create a second review engine.

V2 is initiated by a human through an agent client. It previews a top-level PR review and publishes only after explicit approval. No approval means no GitHub write. Default tests remain no-network; live GitHub smoke tests are opt-in.

Detailed GitHub mapping and publication schemas remain deferred until V2 so they can reflect the actual MCP contract available then.

## V3: trigger and inline review

MCP is not an event trigger. V3 adds a separate trigger layer that receives a PR comment event, parses `/pre-pr-verify`, authorizes the actor, controls duplicate/replayed/concurrent work, and invokes an agent runner using the same Skill and core.

The trigger implementation choice among GitHub Actions, GitHub App, or a small service is intentionally deferred until V3 threat modeling. Inline findings require deterministic mapping to a valid current diff position. An unmappable or stale finding downgrades to the top-level review; line numbers are never guessed.

V3 must preserve V1/V2 behavior and prove bounded credentials, fork isolation, authorization, replay protection, concurrency control, rate-limit behavior, safe publication failure, and deterministic LEFT/RIGHT mapping where applicable.
