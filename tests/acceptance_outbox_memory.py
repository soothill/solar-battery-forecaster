"""Opt-in Linux acceptance: run twice in the same 80 MiB Docker container.

First invocation persists a synthetic 110 MiB backlog on the container disk.
Second invocation starts a fresh Python process, verifies, drains and checks it.
Never use a production state directory. Default /recovery-state is container-local.
"""

from __future__ import annotations

import hashlib
import json
import resource
from datetime import UTC, datetime
from pathlib import Path

# Include the real writer/worker imports in the memory baseline.
from solar_battery_forecaster import operations, storage  # noqa: F401
from solar_battery_forecaster.config import OutboxConfig
from solar_battery_forecaster.outbox import DurableOutbox

STATE = Path("/recovery-state")
RECORDS = 110
RECORD_BYTES = 1_048_576
EXPECTED_HASH = hashlib.sha256(b"m field=1 " + b"x" * (RECORD_BYTES - 10)).hexdigest()


def main() -> None:
    memory_max = Path("/sys/fs/cgroup/memory.max")
    if not memory_max.exists() or memory_max.read_text().strip() != "83886080":
        raise RuntimeError("acceptance requires an enforced 80 MiB cgroup v2 limit")
    phase = "recover" if (STATE / "outbox.sqlite3").exists() else "populate"
    settings = OutboxConfig(state_directory=STATE)
    outbox = DurableOutbox(STATE, settings, "telemetry")
    at = datetime(2026, 9, 6, tzinfo=UTC)
    if phase == "populate":
        for index in range(RECORDS):
            outbox.enqueue(
                property_id=f"fixture-{index % 10}", org="synthetic", bucket="synthetic",
                logical_kind="acceptance", logical_key=str(index),
                min_timestamp=at, max_timestamp=at,
                payload=b"m field=1 " + b"x" * (RECORD_BYTES - 10),
            )
        status = outbox.status()
        if (status.pending_records, status.pending_bytes) != (RECORDS, RECORDS * RECORD_BYTES):
            raise RuntimeError("fixture backlog differs from expected size")
    else:
        if outbox.status().pending_records != RECORDS:
            raise RuntimeError("restart did not preserve the complete backlog")
        confirmed = 0

        def deliver(bucket: str, org: str, payload: str) -> None:
            nonlocal confirmed
            if bucket != "synthetic" or org != "synthetic":
                raise RuntimeError("unexpected delivery destination")
            if hashlib.sha256(payload.encode()).hexdigest() != EXPECTED_HASH:
                raise RuntimeError("recovered payload differs from persisted fixture")
            confirmed += 1

        while outbox.status().pending_records:
            if outbox.drain(deliver, force=True) <= 0:
                raise RuntimeError("recovery made no progress")
        status = outbox.status()
        if confirmed != RECORDS or status.delivered_total != RECORDS:
            raise RuntimeError("recovery lost or duplicated a record")
        if status.quarantined_records or status.blocked_streams:
            raise RuntimeError("valid fixture was quarantined or blocked")
    outbox.close()
    print(json.dumps({
        "phase": phase, "records": RECORDS, "backlog_bytes": RECORDS * RECORD_BYTES,
        "pending_records": status.pending_records,
        "delivered_total": status.delivered_total,
        "rss_peak_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cgroup_memory_peak": Path("/sys/fs/cgroup/memory.peak").read_text().strip(),
        "cgroup_memory_events": Path("/sys/fs/cgroup/memory.events").read_text().strip(),
    }), flush=True)


if __name__ == "__main__":
    main()
