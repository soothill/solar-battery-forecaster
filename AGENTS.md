# Project agent instructions

Before meaningful project work, load all of the following:

- The active role definition in `.agents/<role>.md`.
- The active role memory in `.agents/memory/<role>.md`.
- `.agents/workflow.md`.
- `.agents/process-control-checkpoint.md`.

The project roles are Manager, Product Manager, Architect, Coder, Tester, Security, and
Reviewer. The mandatory delivery flow is:

`Manager -> Product Manager -> Architect -> Coder -> Tester + Security -> Reviewer`

## Agent model selection

Use GPT-6 Astra (`gpt-6-astra`) for Manager, Product Manager, Architect, Coder, Tester,
and Reviewer. Use Daybreak Blue (`gpt-daybreak-blue-latest`) for Security and its security
review workers. This is the user's latest model choice as of 2026-09-06 and supersedes
the earlier all-Astra preference.

Select the role's required model explicitly when launching an agent with a model override.
Full-history forks may inherit a model only from a parent already running the model required
for that role. Preserve the existing reasoning effort. If the required model is unavailable,
report the limitation instead of silently substituting another model. Resume older agents
only when their model matches the role's required model; otherwise create a replacement for
the next concrete task using the same role and memory.

Include the selected model in role handoffs. These instructions govern future launches;
editing them does not change the model of an already-running agent. Role permissions,
independent reviews, and required checks remain unchanged.

## Delivery controls

The Coder may commit and push only to a feature branch and may create or update its pull
request. The Coder cannot push, commit, merge, tag, deploy, or promote `main`. Reviewer is
the only role that may authorize a merge, and only for the exact head independently approved
by Tester and Security with all required GitHub checks passing.

Role prompts do not provide access control. GitHub rulesets, required checks, code ownership,
and separate least-privilege identities provide enforcement. If those controls are absent or
cannot establish separation, agents must describe the gate as advisory and must not claim it
is enforced.

Every non-trivial handoff states which role, memory, workflow, and checkpoint files were
loaded. Memory contains durable, reviewed lessons and decisions only. Never store credentials,
property addresses or coordinates, serial numbers, account identifiers, raw customer payloads,
approval state, or personal data in repository memory.

Keep handoffs concise and batch related findings. Run local preflight before handing off, do not
repeat unchanged gates, and minimize agent turns and GitHub Actions jobs without weakening the
mandatory sequence. Record decisions and evidence, never private chain-of-thought.
