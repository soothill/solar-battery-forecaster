from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "trusted-ci.yml"
LEGACY_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_normal_ci_uses_trusted_base_workflow_and_exact_head_statuses() -> None:
    workflow = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["on"] == {"pull_request_target": {"branches": ["main"]}}
    assert workflow["permissions"] == {}
    assert list(workflow["jobs"]) == ["quality-run", "publish-gates"]
    job = workflow["jobs"]["quality-run"]
    assert job["if"] == (
        "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert job["permissions"] == {"contents": "read"}
    assert job["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "ic-dev",
        "solar-public-ci",
        "isolated",
        "ephemeral",
        "no-private-net",
    ]
    rendered = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "ref: ${{ github.event.pull_request.head.sha }}" in rendered
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"' in rendered
    assert "uv sync --python 3.11 --frozen --all-extras" in rendered
    assert "uv sync --python 3.12 --frozen --all-extras" in rendered
    assert rendered.count("bandit -q -r src") == 1
    assert rendered.count("pip-audit") == 1
    assert 'UV_OFFLINE: "1"' in rendered
    assert "UV_CACHE_DIR: /opt/uv-cache" in rendered
    assert "pip-audit --local --progress-spinner off" in rendered
    assert "--vulnerability-service osv" in rendered
    assert "dependency-graph/compare/$BASE_SHA...$HEAD_SHA" in rendered
    assert "solar_battery_forecaster.dependency_review" in rendered
    publisher = workflow["jobs"]["publish-gates"]
    assert publisher["needs"] == "quality-run"
    assert publisher["if"] == "always()"
    assert publisher["runs-on"] == "ubuntu-latest"
    assert publisher["environment"] == "trusted-status-publisher"
    assert publisher["permissions"] == {}
    assert all("uses" not in step for step in publisher["steps"])
    publish_step = publisher["steps"][0]
    assert publish_step["env"]["STATUS_APP_ID"] == "${{ secrets.STATUS_APP_ID }}"
    assert publish_step["env"]["STATUS_APP_INSTALLATION_ID"] == (
        "${{ secrets.STATUS_APP_INSTALLATION_ID }}"
    )
    assert publish_step["env"]["STATUS_APP_PRIVATE_KEY"] == (
        "${{ secrets.STATUS_APP_PRIVATE_KEY }}"
    )
    publishing = publish_step["run"]
    assert publishing.count('"repos/$GITHUB_REPOSITORY/statuses/$HEAD_SHA"') == 2
    assert "context=intake" in publishing
    assert "context=quality" in publishing
    assert "intake_state=failure" in publishing
    assert "quality_state=failure" in publishing
    assert '[ "$HEAD_REPOSITORY" = "$BASE_REPOSITORY" ]' in publishing
    assert '[ "$QUALITY_RESULT" = success ]' in publishing
    assert "STATUS_APP_PRIVATE_KEY" in publishing
    assert "openssl dgst -sha256 -sign" in publishing
    assert "app/installations/$STATUS_APP_INSTALLATION_ID/access_tokens" in publishing
    assert publishing.count('GH_TOKEN="$installation_token" gh api') == 2
    assert "github.token" not in str(publisher)
    assert "statuses: write" not in rendered
    assert rendered.count("      - uses:") == 1


def test_ci_bootstrap_keeps_existing_controls_until_trusted_workflow_is_live() -> None:
    deployment = (ROOT / "docs" / "self-hosted-ci.md").read_text(encoding="utf-8")
    assert "One-time workflow bootstrap" in deployment
    assert "cannot produce the new `intake` and `quality` statuses" in deployment
    assert "four previously required legacy checks" in deployment
    assert "while both workflow files" in deployment
    assert "both rule sets coexist" in deployment
    legacy = yaml.load(LEGACY_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert legacy["on"] == {"pull_request": {"branches": ["main"]}}
    assert list(legacy["jobs"]) == ["test", "security", "dependency-review"]
    assert legacy["jobs"]["test"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
    ]
    for job in legacy["jobs"].values():
        checkout = next(
            step
            for step in job["steps"]
            if "uses" in step and "checkout" in step["uses"]
        )
        assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
        assert checkout["with"]["persist-credentials"] == "false"
        assert any(
            step.get("run") == 'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"'
            for step in job["steps"]
        )
    legacy_rendered = LEGACY_WORKFLOW.read_text(encoding="utf-8")
    assert legacy_rendered.count("ref: ${{ github.event.pull_request.head.sha }}") == 3
    assert legacy_rendered.count("persist-credentials: false") == 3
    assert legacy_rendered.count('test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"') == 3
    assert "all_external_contributors" in deployment
    assert "Never approve a workflow run originating from a fork" in deployment
    assert "status-only GitHub App" in deployment
    assert "integration/App ID" in deployment


def test_release_provenance_stays_github_hosted() -> None:
    release = yaml.load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert all(job["runs-on"] == "ubuntu-latest" for job in release["jobs"].values())
    deployment = (ROOT / "deployment" / "LXC.md").read_text(encoding="utf-8")
    assert "--deny-self-hosted-runners" in deployment
