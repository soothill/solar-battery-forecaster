# ADR 0003: Immutable interval recommendations and canonical actual energy

Status: proposed implementation; independent review required.

The daily scalar recommendation could credit afternoon solar against earlier load,
and the dashboard independently reconstructed a midnight target. The production
worker now uses one constant-power interval balance from the SoC observation through
the end of the forecast day. UTC defines elapsed durations and persisted timestamps;
the property's timezone defines midnight, schedules and forecast dates.

The configured `expected_kwh_until_next_cheap_window` remains accepted. It is treated
as the forecast day's load total and spread uniformly over that day's elapsed hours;
the same hourly rate covers the evening observation-to-midnight bridge. This is an
explicit assumption, not a learned household profile. The optional internal supplied
load-interval interface supports temporal tests without adding a new configuration
format. Local days may contain 23, 24 or 25 elapsed hours.

Stored energy spans configured minimum to maximum SoC. Reserve is retained above
minimum SoC. Contemporaneous PV supplies load before charging storage; charge and
discharge efficiencies apply on their respective storage flows. Grid charging is
bounded by cheap-slot duration and charge power. A backward requirement pass followed
by a bounded forward simulation reports achievable SoC at every interval end,
residual grid imports and terminal reserve deficit. Constant power within an interval
is an approximation and does not establish subinterval hardware feasibility.

The first contiguous cheap opportunity beginning before local noon is the primary
overnight opportunity, clipped at local noon if its rate remains cheap all day.
Its achievable target has an explicit timestamp. Existing
`grid_charge_kwh` and charge cost describe only that opportunity. The separate
`horizon_grid_charge_kwh` includes later cheap periods within the simulated day.
Capacity and window limitation diagnostics compare the same simulation with unlimited
capacity/power, then bounded capacity, then bounded capacity and power. They are
marginal diagnostic shortfalls; observed grid imports are reported separately.

Forecast persistence retains the observation-day bridge and the forecast day under
one immutable snapshot ID. Daily metadata remains scoped to the forecast day. Exact
raw, corrected and conservative interval energies survive fallback/replay and are
rehydrated; a changed current correction setting does not rewrite an old forecast.
Correction follows AC clipping and corrected output is clipped to inverter AC rating.

New decision identity/version/count fields and `battery_plan` points are additive.
Decision and plan points enter one exact fallback batch, preserving direct-first
delivery. Influx partial-batch visibility is handled by readers requiring the expected
point count and matching property/day/decision/snapshot. The dashboard reads persisted
points rather than recomputing them from current settings. Old records remain readable
as forecast/actual history but do not acquire fabricated interval plans. Rollback can
ignore additive fields; old planner versions must not be presented as production-safe.

Daily PV counters are the canonical actual-energy source for both dashboard and
calibration. A shared evaluator checks finite values, monotonicity, duplicates,
sampling gaps and day boundaries using UTC duration. Partial current days remain
useful but are not calibration data. The 95% coverage threshold remains; extra samples
cannot conceal temporal gaps. No sparse-power bucket integration supplies a substitute
daily counter total. Missing or reset counters make calibration unavailable.

Worker schedules use monotonic start-to-start deadlines, skip missed deadlines and
retain inter-property pacing. No burst catch-up is performed. HTTPS is the default
Influx transport requirement; `allow_insecure_http` is an explicit isolated-test
exception and must not be enabled across production network boundaries.

Five isolated processes, writer-private bounded fallback queues, the read-only
dashboard and recommendation-only behavior are unchanged. This ADR does not authorize
battery control, live vendor-contract assumptions, runner activation or deployment.
