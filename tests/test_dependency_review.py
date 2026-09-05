import pytest

from solar_battery_forecaster.dependency_review import vulnerable_changes


def test_dependency_review_accepts_clean_paginated_diff() -> None:
    assert vulnerable_changes([[{"change_type": "added", "vulnerabilities": []}]]) == 0


def test_dependency_review_counts_vulnerabilities_and_rejects_bad_shape() -> None:
    assert vulnerable_changes([{"vulnerabilities": [{"severity": "high"}]}]) == 1
    with pytest.raises(ValueError, match="invalid"):
        vulnerable_changes([["not-a-change"]])
