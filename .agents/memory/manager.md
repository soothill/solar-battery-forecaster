# Manager memory

- 2026-09-05: Project follows Manager -> Product Manager -> Architect -> Coder -> Tester and
  Security -> Reviewer. Coder commits only to feature branches; Reviewer alone authorizes merge.
- 2026-09-05: Initial release is recommendation-only. Battery control is a separate gated phase.
- 2026-09-05: Writers use direct synchronous InfluxDB delivery while healthy and durably queue the
  exact attempted bytes after failure or behind a property backlog. Disk backpressure stops
  collection without eviction; the accepted direct-to-fallback crash window is documented.
