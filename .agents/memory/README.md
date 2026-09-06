# Agent memory

Role memory preserves durable project knowledge so later work does not rediscover decisions.

- Load the active role memory before meaningful work.
- Record decisions, constraints, recurring defects, safe commands, and operational lessons.
- Keep entries concise and link to durable PRs, ADRs, or issues when available.
- Never store secrets, tokens, property data, raw API payloads, personal data, or approval state.
- Memory changes use the same pull-request gates as code.
- Move superseded chronology to `.agents/memory/archive/<role>/` and preserve provenance.

