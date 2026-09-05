# Runner scaffold

This directory is an installable but intentionally inactive scaffold. Follow
[`docs/self-hosted-ci.md`](../../docs/self-hosted-ci.md) before registration.

The checked-in toolchain manifest pins upstream URLs, checksums, and Python patch releases.
`fetch-artifacts.py` verifies each bounded download; the Dockerfile independently verifies the same
six hashes before installing them. `build-images.sh` also warms the frozen dependency cache and
emits local content-addressed image IDs. Base images and the Ubuntu package snapshot are also pinned
in the manifest. Independently verify and deliberately update those values; never substitute a tag
or floating URL.

Create `solar-ci-isolated` as an internal Docker network. Attach a digest-pinned, hardened proxy
container to that network and a separate outbound bridge, without publishing ports. The proxy is
the only dual-homed component. Review GitHub's current required domains before every image upgrade;
do not add package registries merely to make an unreviewed dependency change pass.

CI copies the image's read-only cache seed into disposable tmpfs, then forces uv offline. A lock
change therefore fails closed until a maintainer reviews the dependency artifacts, rebuilds the
image, and completes staging isolation tests. `pip-audit --local --vulnerability-service osv` audits installed
distributions and may contact the allowlisted OSV API for advisory JSON; package registries remain
blocked and cannot supply code.

`install-host.sh` installs the root-owned systemd supervisor, timer, proxy lifecycle, firewall and
validation helpers, and AppArmor profiles without starting them. Its GitHub App broker reads an
encrypted systemd PEM credential, mints a short-lived installation token, requests a single-use JIT
configuration from the organization endpoint with the restricted group ID, and pipes it to
`run-once.sh`. The same App verifies that repository fork workflows
require approval for `all_external_contributors` and that the non-default organization runner group
named exactly `solar-public-ci` selects only this repository and the protected-main trusted
workflow; personal repositories fail
closed. Operators must never approve fork runs. A separate
status-only App exists solely in the protected GitHub publisher environment and is never installed
on this host. The one-job runner mounts only a disposable tmpfs
volume at `/opt/actions-runner/_work`; the timer re-arms only after it exits. Never persist or log
the JIT value. `accept-host.sh` is the executable structural, egress, cleanup, and fingerprint gate;
the timer cannot start a runner without its current acceptance marker. `uninstall-host.sh` provides
a reversible removal path and preserves credentials for
explicit recovery or secure deletion.
