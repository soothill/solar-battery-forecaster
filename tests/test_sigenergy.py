import json

import pytest

from solar_battery_forecaster.adapters.inverter.sigenergy import (
    MAX_NESTED_DATA_BYTES,
    SigenergyCloud,
    SigenergyError,
    normalize_telemetry,
    validate_endpoint_payload,
)


def test_normalize_sigenergy_payload() -> None:
    result = normalize_telemetry(
        {
            "pvPower": "2.4",
            "gridPower": -0.2,
            "batteryPower": 0.5,
            "loadPower": 1.7,
            "batterySoc": 63,
        },
        {"dailyPowerGeneration": "7.8", "lifetimePowerGeneration": 1234.5},
    )
    assert result.pv_power_kw == 2.4
    assert result.grid_power_kw == -0.2
    assert result.battery_soc_percent == 63
    assert result.daily_pv_kwh == 7.8


def test_nested_sigenergy_data_accepts_bounded_object_string() -> None:
    assert SigenergyCloud._decode_data('{"batterySoc": 50}') == {  # noqa: SLF001
        "batterySoc": 50
    }


@pytest.mark.parametrize("value", ["not-json", "[]", [], None])
def test_nested_sigenergy_data_rejects_invalid_shape(value: object) -> None:
    with pytest.raises(SigenergyError, match="invalid|not an object"):
        SigenergyCloud._decode_data(value)  # noqa: SLF001


def test_nested_sigenergy_data_rejects_large_string() -> None:
    value = json.dumps({"value": "x" * MAX_NESTED_DATA_BYTES})
    with pytest.raises(SigenergyError, match="exceeded its limit"):
        SigenergyCloud._decode_data(value)  # noqa: SLF001

    with pytest.raises(SigenergyError, match="size limit"):
        SigenergyCloud._decode_data(  # noqa: SLF001
            {"value": "x" * MAX_NESTED_DATA_BYTES}
        )


def test_nested_sigenergy_data_rejects_excessive_depth_and_nodes() -> None:
    deep: object = "leaf"
    for _ in range(18):
        deep = {"child": deep}
    with pytest.raises(SigenergyError, match="complexity"):
        SigenergyCloud._decode_data(deep)  # noqa: SLF001

    with pytest.raises(SigenergyError, match="complexity"):
        SigenergyCloud._decode_data({"items": [0] * 10_001})  # noqa: SLF001


def test_nested_sigenergy_data_counts_many_object_separators_at_size_boundary() -> None:
    payload = {f"field-{index:05d}-abcdefghijklm": 0 for index in range(9_000)}
    assert len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_NESTED_DATA_BYTES
    with pytest.raises(SigenergyError, match="size limit"):
        SigenergyCloud._decode_data(payload)  # noqa: SLF001


def test_nested_sigenergy_data_handles_parser_recursion_error() -> None:
    value = "[" * 2_000 + "0" + "]" * 2_000
    with pytest.raises(SigenergyError, match="invalid|not an object"):
        SigenergyCloud._decode_data(value)  # noqa: SLF001


@pytest.mark.parametrize(
    "payload", [{}, {"unknown": 1}, {"batterySoc": []}, {"batterySoc": None}]
)
def test_sigenergy_endpoint_shape_rejects_empty_unknown_or_container_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(SigenergyError, match="recognized fields|field shape|usable fields"):
        validate_endpoint_payload(payload, frozenset({"batterySoc"}), "energy flow")
