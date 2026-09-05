import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "deployment" / "ci-runner"


def test_runner_is_ephemeral_unprivileged_and_hardened() -> None:
    script = (RUNNER / "run-once.sh").read_text(encoding="utf-8")
    required = [
        "@sha256:",
        "--user 10001:10001",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges",
        "apparmor=solar-ci-runner",
        "--pids-limit",
        "--memory",
        "--cpus",
        "type=volume",
        "docker volume rm -f",
        "{{.Internal}}",
        "{{.State.Running}}",
        "{{.Config.User}}",
        "{{json .HostConfig.PortBindings}}",
        "{{.Config.Image}}",
        ".NetworkSettings.Networks",
        "target=/opt/actions-runner/_work",
    ]
    assert all(value in script for value in required)
    assert "docker.sock" not in script
    assert "--publish" not in script
    assert "type=bind" not in script
    assert "sha256:????????" in script
    assert "--tmpfs /opt/uv-cache:" in script
    entrypoint = (RUNNER / "entrypoint.sh").read_text(encoding="utf-8")
    assert "cp -a /opt/uv-cache-seed/. /opt/uv-cache/" in entrypoint
    assert "docker create" in script
    assert "validate-runner.sh" in script
    assert "docker start --attach --interactive" in script


def test_proxy_policy_denies_private_destinations_and_allows_only_github() -> None:
    policy = (RUNNER / "squid.conf").read_text(encoding="utf-8")
    for network in ["10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "fc00::/7"]:
        assert network in policy
    assert "http_access deny private" in policy
    assert "acl private dst " in policy
    assert "http_access allow github" in policy
    assert "acl advisory dstdomain api.osv.dev" in policy
    assert "http_access allow advisory" in policy
    assert policy.index("http_access deny private") < policy.index("http_access allow github")
    assert policy.rstrip().endswith("forwarded_for delete")
    proxy_build = (RUNNER / "Dockerfile.proxy").read_text(encoding="utf-8")
    assert "squid -k parse -f /etc/squid/squid.conf" in proxy_build
    assert "HEALTHCHECK" in proxy_build


def test_root_supervisor_uses_encrypted_app_credential_and_one_job_jit() -> None:
    service = (RUNNER / "solar-ci-runner.service").read_text(encoding="utf-8")
    timer = (RUNNER / "solar-ci-runner.timer").read_text(encoding="utf-8")
    broker = (RUNNER / "mint-github-app-token.sh").read_text(encoding="utf-8")
    acquire = (RUNNER / "acquire-and-run.sh").read_text(encoding="utf-8")
    assert "LoadCredentialEncrypted=github-app-key:" in service
    assert "/usr/bin/flock --nonblock" in service
    assert "RuntimeMaxSec" not in service
    assert "ConditionPathExists=/var/lib/solar-ci-runner/acceptance.ok" in service
    assert "ExecCondition=/usr/local/libexec/solar-ci-runner/check-acceptance.sh" in service
    assert "ExecCondition=/usr/local/libexec/solar-ci-runner/verify-fork-policy.sh" in service
    assert "ExecStartPre=/usr/local/libexec/solar-ci-runner/validate-host.sh" in service
    assert "StartLimitIntervalSec=30min" in service
    assert "StartLimitBurst=6" in service
    assert "OnUnitInactiveSec" in timer
    assert "OnUnitInactiveSec=2min" in timer
    assert "RandomizedDelaySec=60sec" in timer
    assert "openssl dgst -sha256 -sign" in broker
    assert "access_tokens" in broker
    assert "generate-jitconfig" in acquire
    assert "actions/runners/$runner_id" in acquire
    assert acquire.count("$TOKEN_COMMAND 2>/dev/null") == 2
    assert "cleanup_required=true" in acquire
    assert acquire.index("generate-jitconfig") < acquire.index("cleanup_required=true", 100)
    assert acquire.index("cleanup_required=true", 100) < acquire.index("encoded_jit_config")
    for label in ["ic-dev", "solar-public-ci", "isolated", "ephemeral", "no-private-net"]:
        assert f"labels[]={label}" in acquire


def test_proxy_lifecycle_and_validation_are_fail_closed() -> None:
    up = (RUNNER / "proxy-up.sh").read_text(encoding="utf-8")
    validation = (RUNNER / "validate-host.sh").read_text(encoding="utf-8")
    down = (RUNNER / "proxy-down.sh").read_text(encoding="utf-8")
    assert "docker network create --internal" in up
    assert "iptables -I DOCKER-USER" in up
    assert "iptables -I INPUT" in up
    for requirement in [
        ".AppArmorProfile",
        ".HostConfig.CapDrop",
        ".HostConfig.PidsLimit",
        ".HostConfig.Memory",
        ".HostConfig.NanoCpus",
        ".State.Health.Status",
        "profile=builtin",
        "{{len .Mounts}}",
    ]:
        assert requirement in validation
    assert "docker network rm" in down
    assert "iptables -D INPUT" in down


def test_runner_contract_is_inspected_before_untrusted_process_starts() -> None:
    validation = (RUNNER / "validate-runner.sh").read_text(encoding="utf-8")
    for requirement in [
        "{{.Image}}",
        "{{.HostConfig.ReadonlyRootfs}}",
        "{{.HostConfig.Privileged}}",
        "{{.AppArmorProfile}}",
        "{{json .HostConfig.CapDrop}}",
        "{{.HostConfig.PidsLimit}}",
        "{{.HostConfig.Memory}}",
        "{{.HostConfig.NanoCpus}}",
        "seccomp=unconfined",
        "{{len .Mounts}}",
        "/opt/actions-runner/_work",
        "{{.Internal}}",
    ]:
        assert requirement in validation


def test_toolchain_manifest_is_exact_and_build_reverifies_artifacts() -> None:
    manifest = json.loads((RUNNER / "toolchain.json.example").read_text(encoding="utf-8"))
    assert manifest["python311_version"].count(".") == 2
    assert manifest["python312_version"].count(".") == 2
    for artifact in manifest["artifacts"].values():
        assert artifact["url"].startswith("https://github.com/")
        assert len(artifact["sha256"]) == 64
        int(artifact["sha256"], 16)
    dockerfile = (RUNNER / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("sha256sum --check --strict") == 6
    assert "toolchain.tar.gz" not in dockerfile
    assert "uv python install" not in dockerfile
    assert "/opt/python/3.11/bin/python3.11" in dockerfile
    assert "/opt/python/3.12/bin/python3.12" in dockerfile
    assert "uv sync --directory /tmp/project --frozen --all-extras" in dockerfile
    build = (RUNNER / "build-images.sh").read_text(encoding="utf-8")
    assert "docker image inspect --format '{{.Id}}'" in build
    assert "RUNNER_IMAGE=%s" in build
    assert "PROXY_IMAGE=%s" in build


def test_all_runner_shell_scripts_parse() -> None:
    for path in RUNNER.glob("*.sh"):
        subprocess.run(["sh", "-n", path], check=True)


def test_install_is_inactive_and_uninstall_preserves_credentials() -> None:
    install = (RUNNER / "install-host.sh").read_text(encoding="utf-8")
    uninstall = (RUNNER / "uninstall-host.sh").read_text(encoding="utf-8")
    assert "systemctl daemon-reload" in install
    assert "systemctl enable" not in install
    assert "systemctl start" not in install
    assert "accept-host.sh" in install
    assert "check-acceptance.sh" in install
    assert "verify-fork-policy.sh" in install
    assert "solar-ci-policy-check.service" in install
    assert "rm -f /var/lib/solar-ci-runner/acceptance.ok" in install
    assert "systemctl disable --now solar-ci-runner.timer" in uninstall
    assert "/etc/solar-ci-runner" not in "\n".join(
        line for line in uninstall.splitlines() if line.lstrip().startswith("rm ")
    )


def test_apparmor_allows_required_executable_mappings() -> None:
    policy = (RUNNER / "solar-ci-runner.apparmor").read_text(encoding="utf-8")
    for path in [
        "/opt/actions-runner/** mrix,",
        "/opt/python/** mrix,",
        "/opt/actions-runner/_work/** mrwkix,",
        "/opt/uv-cache/** mrwk,",
    ]:
        assert path in policy


def test_acceptance_is_executable_fail_closed_and_cleans_up() -> None:
    acceptance = (RUNNER / "accept-host.sh").read_text(encoding="utf-8")
    checker = (RUNNER / "check-acceptance.sh").read_text(encoding="utf-8")
    probe = (RUNNER / "acceptance-probe.py").read_text(encoding="utf-8")
    ast.parse(probe)
    for requirement in [
        "systemctl start solar-ci-proxy.service",
        "systemctl start solar-ci-policy-check.service",
        "validate-host.sh",
        "validate-runner.sh",
        "--read-only",
        "--cap-drop ALL",
        "apparmor=solar-ci-runner",
        "docker volume rm -f",
        "! docker inspect",
        "! docker volume inspect",
        "acceptance.ok",
        "$CHECK_ACCEPTANCE --print",
    ]:
        assert requirement in acceptance
    assert acceptance.index("systemctl start solar-ci-policy-check.service") < acceptance.index(
        "$CHECK_ACCEPTANCE --print"
    )
    assert "sha256sum" in checker
    assert "fingerprint=" in checker
    for destination in [
        "api.github.com",
        "api.osv.dev",
        "127.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
    ]:
        assert destination in probe
    assert (RUNNER / "Dockerfile").read_text(encoding="utf-8").count(
        "solar-ci-acceptance-probe.py"
    ) >= 2


def test_fork_workflow_policy_is_verified_before_runner_activation() -> None:
    verifier = (RUNNER / "verify-fork-policy.sh").read_text(encoding="utf-8")
    policy_unit = (RUNNER / "solar-ci-policy-check.service").read_text(encoding="utf-8")
    marker = (RUNNER / "check-acceptance.sh").read_text(encoding="utf-8")
    assert "actions/permissions/fork-pr-contributor-approval" in verifier
    assert '!= all_external_contributors' in verifier
    assert "$TOKEN_COMMAND" in verifier
    assert "LoadCredentialEncrypted=github-app-key:" in policy_unit
    assert "verify-fork-policy.sh" in policy_unit
    assert "solar-ci-policy-check.service" in marker
    assert "verify-fork-policy.sh" in marker
