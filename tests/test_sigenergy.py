from solar_battery_forecaster.adapters.inverter.sigenergy import normalize_telemetry


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

