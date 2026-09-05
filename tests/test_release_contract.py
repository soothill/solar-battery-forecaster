import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
FULL_SHA_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
ATTEST_ACTION = "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"


def load_release_workflow() -> dict[str, object]:
    return yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_is_tag_only_least_privilege_and_sha_pinned() -> None:
    workflow = load_release_workflow()
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    build = jobs["build-attest"]
    publish = jobs["publish"]
    assert build["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }
    assert publish["permissions"] == {"actions": "read", "contents": "write"}
    assert publish["needs"] == "build-attest"
    assert publish["environment"] == "release"

    action_refs = [
        step["uses"]
        for job in jobs.values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert ATTEST_ACTION in action_refs
    assert all(FULL_SHA_ACTION.fullmatch(action) for action in action_refs)


def test_release_verifies_source_and_builds_attested_assets() -> None:
    workflow = load_release_workflow()
    build_steps = workflow["jobs"]["build-attest"]["steps"]
    publish_steps = workflow["jobs"]["publish"]["steps"]
    build_commands = "\n".join(step.get("run", "") for step in build_steps)
    publish_commands = "\n".join(step.get("run", "") for step in publish_steps)

    assert "git merge-base --is-ancestor" in build_commands
    assert 're.fullmatch(r"v[0-9]+\\.[0-9]+\\.[0-9]+", tag)' in build_commands
    assert 'tag != f"v{version}"' in build_commands
    assert "refs/remotes/origin/main" in build_commands
    assert ".commit.verification.verified" in build_commands
    assert 'test "$verification" = "true"' in build_commands
    assert "uv sync --frozen --all-extras" in build_commands
    assert "hatchling') == '1.32.0'" in build_commands
    assert "uv run --frozen pytest" in build_commands
    assert "uv run --frozen bandit" in build_commands
    assert "uv run --frozen pip-audit" in build_commands
    assert "python -m build --no-isolation" in build_commands
    assert "sha256sum *.whl *.tar.gz > SHA256SUMS" in build_commands
    assert "gh release create" in publish_commands

    attest_step = next(step for step in build_steps if step.get("uses") == ATTEST_ACTION)
    subjects = attest_step["with"]["subject-path"]
    assert "dist/*.whl" in subjects
    assert "dist/*.tar.gz" in subjects
    assert "dist/SHA256SUMS" in subjects


def test_deployment_requires_exact_attestation_identity_before_checksum() -> None:
    deployment = (ROOT / "deployment" / "LXC.md").read_text(encoding="utf-8")
    attestation = deployment.index("gh attestation verify")
    checksum = deployment.index("sha256sum --check SHA256SUMS")

    assert attestation < checksum
    assert '--repo "$repository"' in deployment
    assert (
        "--signer-workflow "
        "soothill/solar-battery-forecaster/.github/workflows/release.yml"
    ) in deployment
    assert '--source-ref "refs/tags/$release_version"' in deployment
    assert '--source-digest "$source_commit"' in deployment
    assert "--deny-self-hosted-runners" in deployment
    assert "only a supplemental corruption/completeness check" in deployment
    assert "trusted administrator workstation" in deployment
    assert "gh attestation verify --help" in deployment
    assert "Debian's base" in deployment
    assert "transfer the three verified files" in deployment


def test_shared_application_tree_explicitly_removes_group_write() -> None:
    deployment = (ROOT / "deployment" / "LXC.md").read_text(encoding="utf-8")
    assert "chmod -R g-w /opt/solar-battery-forecaster" in deployment
