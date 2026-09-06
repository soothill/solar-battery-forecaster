# Manager memory

- 2026-09-06: The user's latest choice is GPT-6 Astra (`gpt-6-astra`) for Manager,
  Product Manager, Architect, Coder, Tester, and Reviewer, with Daybreak Blue
  (`gpt-daybreak-blue-latest`) for Security and its security review workers. This supersedes
  the earlier all-Astra preference. Select the required model for subsequent launches;
  replace mismatched-model agents when their next concrete task begins. Existing processes
  do not change model when policy files are edited. Preserve review independence and
  reasoning effort; report unavailable models without silently substituting another model.

- 2026-09-05: Project follows Manager -> Product Manager -> Architect -> Coder -> Tester and
  Security -> Reviewer. Coder commits only to feature branches; Reviewer alone authorizes merge.
- 2026-09-05: Initial release is recommendation-only. Battery control is a separate gated phase.
- 2026-09-05: Writers use direct synchronous InfluxDB delivery while healthy and durably queue the
  exact attempted bytes after failure or behind a property backlog. Disk backpressure stops
  collection without eviction; the accepted direct-to-fallback crash window is documented.
