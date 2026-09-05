import tomllib
from pathlib import Path


def test_build_backend_is_exactly_pinned_in_frozen_dev_environment() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = project["build-system"]["requires"]
    dev_requirements = project["project"]["optional-dependencies"]["dev"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked_packages = {package["name"]: package for package in lock["package"]}

    assert build_requirements == ["hatchling==1.32.0"]
    assert "hatchling==1.32.0" in dev_requirements
    assert locked_packages["hatchling"]["version"] == "1.32.0"
    project_dev = locked_packages["solar-battery-forecaster"]["optional-dependencies"]["dev"]
    assert {"name": "hatchling"} in project_dev


def test_ci_build_disables_dependency_resolution_isolation() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "trusted-ci.yml"
    ).read_text(encoding="utf-8")

    assert "uv sync --python 3.11 --frozen --all-extras" in workflow
    assert "uv sync --python 3.12 --frozen --all-extras" in workflow
    assert "python -m build --no-isolation" in workflow
