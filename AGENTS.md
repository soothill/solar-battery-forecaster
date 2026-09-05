# Project agent instructions

Before meaningful project work, load all of the following:

- The active role definition in `.agents/<role>.md`.
- The active role memory in `.agents/memory/<role>.md`.
- `.agents/workflow.md`.
- `.agents/process-control-checkpoint.md`.

The project roles are Manager, Product Manager, Architect, Coder, Tester, Security, and
Reviewer. The mandatory delivery flow is:

`Manager -> Product Manager -> Architect -> Coder -> Tester + Security -> Reviewer`

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

