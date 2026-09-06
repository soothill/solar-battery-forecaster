# Production readiness and acceptance

The application is recommendation-only. Do not use its output to automate battery control.
This checklist separates source/test completion from live installation approval; it is not an
approval record. Exact candidate evidence belongs in the PR and process-control checkpoint.

## Release prerequisites

- **Sigenergy vendor access:** still pending. Obtain Monitoring-only credentials, confirm official
  response fields, units, observation time, online/stale behaviour and installed capacities using
  the [credential guide](setup-and-credentials.md). Fixture normalization does not prove any of
  these on a customer's inverter. Local Modbus and inverter writes are unsupported.
- **Per-property inputs:** confirm usable (not nominal) battery capacity, minimum/maximum SoC,
  charge-power limit, panel geometry, timezone, reserve and daily load estimate. Compare a complete
  observed day and multiple overnight recommendations with the vendor app before relying on them.
- **Octopus:** verify the exact tariff's full timeline, VAT-inclusive rates and cheap threshold.
  Standard-rate REST support does not include Intelligent bonus dispatches. Do not interpret a
  missing interval as zero cost or as permission to charge.
- **Secure operation:** certificate-verified InfluxDB HTTPS, scoped service tokens, authenticated
  HTTPS mobile access, private state directories, retention, backups and restore checks must pass
  on the actual target LXC. The HTTP exception is for disposable isolated testing only.
- **Capacity and reliability:** run sustained collection plus backlog recovery under the supplied
  service limits. Verify recent useful telemetry, tariff and plan timestamps, not just heartbeats.
  Complete the guide's scoped outage, restart, restore and rollback acceptance. Never interrupt a
  shared database to test this application.
- **Release controls:** exact-head Tester and Daybreak Security evidence, Reviewer authorization,
  required GitHub checks and an attested release must all be available before promotion.

## One human owner

One person can operate the project and make its final release decision. Product, Architecture,
Coder, Tester, Security and Reviewer are distinct agent roles, not additional humans. Independent
agent reviews provide evidence but do not create separate GitHub identities or prevent an
administrator from bypassing controls.

Keep required checks and existing branch protections intact. Do not claim an owner's approval of
their own pull request satisfies GitHub's independent approving-review rule. If current protection
requires an approval the author cannot supply, the merge remains blocked until a separately
authorized review identity or an explicitly designed and approved solo-maintainer policy exists.
Never silently lower that requirement or use administrator bypass. A separate second human is
not needed to continue source fixes, isolated tests or to prepare a release candidate.

The [local-runner design](self-hosted-ci.md) presently requires an organization-owned repository and
restricted runner group plus separate least-privilege GitHub Apps. A personal-owner repository
does not satisfy that design; leave the runner inactive. Moving the repository, creating Apps and
changing protected policies require an explicit owner decision. Local preflight on `ic-dev` is
still possible without registering it as a public GitHub Actions runner. Ordinary builds target
the isolated local runner after acceptance; the small status publisher and tag-only provenance
workflow remain hosted. Do not move attestation builds locally without redesigning and reviewing
the release trust policy.

## Reading plans and actuals

The planner simulates timestamped solar, configured load and cheap-rate intervals in UTC, with
property-local day boundaries. It reports the primary overnight target/charge/cost separately from
full-horizon charging and unavoidable grid imports. Capacity and charge-window shortfall measures
can overlap and must not be added together. The configured load is a model, not a learned fact.

An old decision without a complete matching trajectory cannot be shown as a new interval plan.
Decision, trajectory and forecast must share their immutable identities. The next scheduled plan
produces new-format records; older forecasts and actuals remain useful. Actual daily energy is
coverage-qualified; sparse, reset or incomplete counters must not silently train a correction
factor. A fresh page or running process is not evidence that a provider reading is fresh.

## Opt-in real InfluxDB test

`tests/test_influx_integration.py` defaults to skipped. To run it, create a new disposable InfluxDB
2.x instance with no existing volume, bound only to loopback (or an SSH loopback forward). Set
`SOLAR_TEST_DISPOSABLE_INFLUX_URL` to that bare HTTP endpoint and run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_influx_integration.py -q
```

The fixture refuses an already initialized server before mutation, generates ephemeral credentials,
creates three synthetic buckets and tests direct writes, real connection refusal, process restart,
replay and duplicate-point idempotency. It does not stop the database or test vendor acceptance.
Afterward remove only the explicitly named disposable container and its test volumes. A second run
needs a fresh uninitialized instance. Never set this variable to a production/shared endpoint.

`tests/acceptance_outbox_memory.py` is a separate opt-in Linux recovery harness. It refuses to run
without a cgroup v2 `memory.max` of exactly 80 MiB. Run it twice as a non-root user in a disposable
container with no host mounts, no network, no additional capabilities and no swap: first populate
the synthetic 110 MiB backlog, then start a fresh process to verify and drain it. Its fixed
`/recovery-state` directory must be container-local and empty before the first invocation. Inspect
both exit codes and cgroup OOM events, then remove the exact test container. Disk page-cache pressure
can reach the memory ceiling without an OOM; record peak process RSS and OOM counters separately.

Use a single batched local preflight before pushing a candidate. Do not rerun unchanged gates or
enable duplicate push/PR Actions triggers. Record evidence and unresolved gates without credentials,
addresses, serial numbers or customer payloads.
