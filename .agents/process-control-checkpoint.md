# Process-control checkpoint

Every non-trivial pull request must answer these items before Reviewer authorization.

## Candidate

- PR number and URL
- Full candidate head SHA
- Source branch and target branch
- Coder identity and handoff time

## Product and architecture

- Product Manager acceptance criteria and disposition
- Architect contract/ADR disposition
- User-facing and data-migration impact

## Verification

- Tester decision for the exact SHA, commands, results, and known gaps
- Security decision for the exact SHA, threat surfaces, scanner results, and known risks
- Required GitHub checks are current and passing
- All blocking review threads are resolved

## Operations and privacy

- Rollout, rollback, observability, and failure behavior
- Secrets, personal/property data, retention, and egress impact
- Dependency and deployment impact
- Battery-control impact; `None` unless the dedicated control gate is satisfied

## Reviewer authorization

Reviewer records either `APPROVED` or `CHANGES_REQUESTED`, the exact SHA, Tester result,
Security result, required-check result, and unresolved risk. A new commit invalidates the record.

