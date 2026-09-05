from pathlib import Path

import pytest

from solar_battery_forecaster.config import load_config


def test_missing_environment_variable_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
influxdb:
  url: http://localhost:8086
  org: test
  token: ${NOT_SET_FOR_TEST}
properties: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NOT_SET_FOR_TEST"):
        load_config(path)

