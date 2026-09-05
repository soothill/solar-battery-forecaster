# Agent workflow

## Required sequence

1. Manager records scope, risk, owners, and the delivery branch.
2. Product Manager records outcomes and testable acceptance criteria.
3. Architect records affected contracts, data flows, and any ADR requirement.
4. Coder implements on a feature branch, adds tests, and opens or updates a PR.
5. Tester tests the exact candidate head without editing it and records pass or changes requested.
6. Security reviews the same exact head without editing it and records pass or changes requested.
7. Reviewer verifies current Tester and Security evidence, required checks, unresolved feedback,
   and exact-head identity before authorizing merge.

Tester and Security can run in parallel. Any new commit invalidates both conclusions. Failed or
missing evidence returns to Manager, who routes changes back through Coder. Reviewer never fixes
code inside the final gate.

## Git boundaries

- Coder: feature branches and pull requests only.
- Tester and Security: read-only review of the candidate head; test/security files are changed
  only in a separate follow-up Coder pass.
- Reviewer: approval and merge authorization only after both gates pass.
- Manager: coordination; cannot waive or bypass a gate.
- No role pushes directly to `main`, force-pushes protected refs, or bypasses branch rules.

## Exact-head evidence

Every handoff identifies the full 40-character candidate SHA. Reviews and CI results for older
SHAs are stale. GitHub checks and review records are authoritative; PR-body text and repository
memory are informational.

## Security-sensitive changes

Authentication, secrets, property data, hosted multi-tenancy, dependencies, external egress,
deployment, Influx queries, Sigenergy/Octopus adapters, and any battery control require Security
review. Battery control additionally requires the controls in `SECURITY.md`.

## Delivery efficiency

Run fast local checks before handoff. Batch related findings into one Coder correction pass.
Do not repeat a full gate when only non-executable documentation changes and the relevant owner
confirms the candidate behavior is byte-identical.

Use concise, evidence-based role handoffs and report conclusions, commands, outcomes, and blockers;
do not store or request private chain-of-thought. Minimize agent turns by batching findings and
using local preflight before asking another role to evaluate an exact candidate. Do not rerun an
unchanged role gate or unchanged CI job merely to produce duplicate evidence.

Minimize GitHub Actions jobs and triggers while retaining required independent evidence. Normal CI
uses a protected-base `pull_request_target` definition: one sequential same-repository-only quality
job on the ephemeral isolated `ic-dev` runner and one hosted no-checkout publisher for exact-head
`intake` and `quality` statuses. Its workflow token has no permissions; a distinct status-only App
credential is restricted to its protected environment. Untrusted quality code has only
`contents: read`. All external fork workflows require approval and must never be approved, so fork
heads have absent required statuses and cannot reach
the runner; reviewed changes are imported into the base repository and submitted in a replacement
PR. Release provenance continues to use GitHub-hosted runners.

## Memory

Load the active role memory before work. Update it at durable decisions, material review findings,
and handoff outcomes. Archive superseded chronology according to `.agents/memory/README.md`.
