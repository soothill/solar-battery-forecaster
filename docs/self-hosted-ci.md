# Isolated CI runner on `ic-dev`

Ordinary CI is anchored to protected `main` with `pull_request_target`. The exact immutable event
origin permits `quality-run` only for same-repository heads; that sequential untrusted-code job runs
on `ic-dev`, `solar-public-ci`, `isolated`, `ephemeral`, `no-private-net` and has only
`contents: read`. An always-running hosted `publish-gates` job performs no checkout and gives its
workflow token only `contents: read` for dependency-graph and Git-tree API reads. That token cannot
write statuses. Inside the protected
`trusted-status-publisher` environment the job mints a short-lived installation token from a
second, status-only GitHub App and uses only that App token to post final `intake` and `quality`
statuses to the exact PR head. Protect those contexts with that App's integration ID, not merely
their names or the workflow job names.

Repository Actions policy must be `all_external_contributors`, and maintainers must never approve a
fork workflow run. Fork pull requests therefore receive no required exact-head statuses and never
reach `ic-dev`; the absence blocks merging. A maintainer must review the
complete patch and provenance without executing it, import the acceptable commit onto a new branch
in the base repository, and open a replacement same-repository pull request. Never use
`pull_request_target` to check out fork code: the workflow definition and publisher come from
protected `main`, and only an origin-approved same-repository head reaches the isolated runner.

## Host and runner model

Use `deployment/ci-runner/` to build a dedicated image, record its immutable image digest, and run
exactly one GitHub Actions job per container through a JIT or `--ephemeral` registration. The
root-owned supervisor creates a fresh named work volume, starts the container as UID/GID 10001, and
destroys both container and work volume after exit. It must never mount the Docker socket, host
paths, SSH agents, cloud credentials, production secrets, or service ports.

The container is read-only except for size-limited `tmpfs` paths and its disposable work volume. It
drops all capabilities, uses `no-new-privileges`, the Docker default seccomp policy and the installed
`solar-ci-runner` AppArmor profile, and has CPU, RAM, and PID limits. Bake Python 3.11/3.12, uv,
gitleaks, the locked project toolchain, and the Actions runner into the image; CI itself uses only
the full-SHA-pinned checkout action and local commands.

The baked toolchain includes a populated, read-only uv cache seed. Each runner copies it into a
bounded disposable tmpfs cache before forcing offline frozen syncs. A dependency/lock change fails
until a maintainer reviews its artifacts and rebuilds the runner image in staging. Pip-audit audits
the installed environment; only advisory JSON from the explicitly allowlisted OSV API is reachable.
Package indexes stay blocked.

The image also retains a root-owned, read-only `/opt/ci-tools` environment containing the verified
uv and gitleaks binaries, a frozen Bandit/pip-audit/Ruff virtual environment, and the reviewed
gitleaks configuration. Immediately after exact-head checkout, the trusted workflow records HEAD
and its Git tree digest, requires a clean tree, and runs gitleaks, Bandit, lock validation,
dependency export/audit, and Ruff only from `/opt/ci-tools`. It does not install or execute candidate
code first. That completed evidence step emits the clean source tree digest before any candidate
execution. Tests and the no-isolation build then run in a separate disposable copy with GitHub
command-file variables removed. Dependency synchronization uses `--no-install-project`, so it
cannot invoke the candidate build backend or rely on an editable-install `.pth`; tests import the
candidate explicitly through the disposable copy's `src` directory. Only the subsequent direct
Python 3.12 no-isolation build invokes the candidate packaging backend. A final step requires the
original checkout to retain the exact HEAD/tree digest and have no modifications or untracked
files; it cannot replace or modify the pre-candidate evidence output consumed by the publisher.

Dependency-graph comparison is not evaluated by candidate Python. The hosted protected publisher
uses only its read-only normal workflow token to fetch the exact base-to-head comparison and trusted
inline `jq` to reject malformed results or any vulnerability. It independently resolves the remote
Git tree and compares it with the pre-candidate evidence output. `quality` becomes successful only
when the complete local isolated job, pre-candidate evidence, remote tree comparison, and hosted
dependency comparison all pass. Its distinct status App token is used only for the two status
writes.

The runner joins an internal Docker network and can reach only a separately hardened HTTPS CONNECT
proxy. The proxy allowlist must cover the current GitHub Actions endpoints needed by the runner and
checkout; it denies loopback, link-local, RFC1918, carrier-grade NAT, IPv6 local/link-local, and
Tailscale ranges after DNS resolution. It publishes no host port. Fail closed if the proxy,
AppArmor profile, internal network, immutable image digest, or egress acceptance tests are absent.

GitHub runner workflow restrictions require an organization-level runner group. The current
personal-account repository cannot activate this runner safely. Before installation, transfer or
recreate the repository under an organization, create a non-default runner group named exactly
`solar-public-ci`, select only this
repository, and restrict the group to exactly
`ORGANIZATION/REPOSITORY/.github/workflows/trusted-ci.yml@refs/heads/main`. The policy verifier
derives that identity from `REPOSITORY` and rejects a personal owner, the default group, additional
repositories or workflows, and any visibility other than `selected`.

## Installation and acceptance

Required host packages are Docker Engine with AppArmor and the default seccomp profile, systemd,
iptables with `DOCKER-USER`, Python 3, jq, OpenSSL, and a current GitHub CLI. Docker access remains
root-only. The runner base image digest must provide the documented Actions runner native
dependencies and Git; the proxy base digest must provide Squid running as numeric UID 10001.

1. Copy `toolchain.json.example` to a root-owned review file. Its runner and Squid base-image
   digests, Ubuntu package snapshot, Actions runner, uv, GitHub CLI, gitleaks, standalone Python
   artifacts, and Python patch versions are exact; independently confirm their current upstream
   provenance and checksums before each rebuild. `build-images.sh` downloads only
   the manifest URLs, verifies every SHA-256 in both the fetcher and Docker build, installs exact
   Python versions through the verified uv binary, warms the frozen dependency cache, and writes
   content-addressed image IDs to `images.env.candidate`. Scan and stage-test both images.
2. Under the organization owner, create a non-default organization runner group named exactly
   `solar-public-ci` with visibility `selected`. Select this repository and no other repository. Enable workflow restrictions and
   select only `ORGANIZATION/REPOSITORY/.github/workflows/trusted-ci.yml@refs/heads/main`. Record the
   numeric group ID; never use the default group. Run `install-host.sh`. It installs root-owned
   helpers, AppArmor policies, and systemd units but
   deliberately starts nothing. Copy the reviewed candidate image IDs to root-owned mode-0600
   `/etc/solar-ci-runner/images.env` and fill `runner.env` with repository, numeric runner-group ID,
   the fixed `RUNNER_GROUP_NAME=solar-public-ci`,
   GitHub App ID, and installation ID.
3. Set the repository's fork pull-request workflow approval policy to
   `all_external_contributors`. Never approve a workflow run originating from a fork. Verify it with
   the GitHub API endpoint
   `GET /repos/OWNER/REPO/actions/permissions/fork-pr-contributor-approval`; the response must be
   exactly `{"approval_policy":"all_external_contributors"}`. The installed
   `solar-ci-policy-check.service`, host acceptance, and every runner start perform this same check
   using the runner-registration App and fail closed before requesting JIT configuration. Keep the
   timer disabled: `accept-host.sh` cannot create its activation marker until this policy check
   succeeds.
4. Create a dedicated GitHub App installed on the organization but selected for only this
   repository. Grant organization self-hosted-runners write for group inspection and organization-
   scoped JIT/list/delete operations, plus repository metadata read and the minimum repository
   Administration read needed for the fork-approval policy endpoint. Grant no contents, statuses,
   secrets, or deployment permission.
   Do not use an existing broad gh OAuth token. Encrypt its PEM private key for this host without
   committing it:

   ```bash
   systemd-creds encrypt --name=github-app-key app-private-key.pem \
     /etc/credstore.encrypted/solar-ci-runner-github-app-key
   chmod 0600 /etc/credstore.encrypted/solar-ci-runner-github-app-key
   ```

   Systemd exposes the decrypted key only in the service credential directory. The root-owned
   broker creates a nine-minute App JWT, exchanges it for a short-lived installation token, calls
   GitHub's organization-scoped JIT runner API with the restricted group ID, and pipes the one-use
   encoded configuration directly to the container.
   Neither credential is written to disk, logged, placed in the image, or passed to a workflow.
5. Create a second GitHub App installed only on this repository. It must have only metadata read and
   commit-statuses write permission: no Actions administration, contents, deployments, secrets, or
   runner-management permission. Create a protected GitHub environment named
   `trusted-status-publisher`, restrict its deployment branches/tags to protected `main` only, and
   store this second App's values as environment secrets `STATUS_APP_ID`,
   `STATUS_APP_INSTALLATION_ID`, and `STATUS_APP_PRIVATE_KEY`. Never reuse or copy the runner App,
   installation, or PEM. The hosted publisher's normal `GITHUB_TOKEN` has only `contents: read` for
   dependency-graph and Git-tree API reads; the job performs no checkout, and that token cannot
   write statuses.
6. Run root-owned `/usr/local/libexec/solar-ci-runner/accept-host.sh`. This executable acceptance
   starts and structurally validates the proxy, launches the digest-pinned runner image with the
   production restrictions, proves GitHub and OSV CONNECT succeed, proves representative
   loopback, IPv6-local, RFC1918, Tailscale/CGNAT, metadata and non-allowlisted destinations are
   denied, proves direct egress fails, and verifies container/work-volume cleanup. It creates
   `/var/lib/solar-ci-runner/acceptance.ok` only after every check passes. The marker binds accepted
   configs, units, AppArmor profiles, and helpers by SHA-256; changing or reinstalling them
   invalidates activation until acceptance is rerun. The runner service uses both
   `ConditionPathExists` and an `ExecCondition` fingerprint check.
7. Test that an unapproved fork PR leaves exact-head `intake` and `quality` absent and creates no
   runner job, and that a same-repository PR produces one successful pair for the exact SHA. Only
   then make both status contexts required and bind each required context to the second App's exact
   integration/App ID. Also confirm `main` rules and the protected environment restrict the workflow
   and status credential. A same-named status from GitHub Actions or another App must not satisfy the
   ruleset.

After acceptance, enable `solar-ci-runner.timer`. Systemd serializes starts with a root-only flock.
One JIT runner waits for one job without an arbitrary idle timeout, exits after that job, destroys
its tmpfs-backed work volume, and is re-armed by the timer. The workflow checks out the exact PR
head SHA and asserts `git rev-parse HEAD` before executing it. Failed acquisition/start attempts
back off for two minutes plus up to sixty seconds of jitter and are capped at six starts per thirty
minutes. If startup fails after JIT allocation, the supervisor obtains a fresh installation token
and best-effort deletes the matching stale runner registration; a generic warning requires manual
inspection when cleanup cannot complete.

Rotate the supervisor's GitHub App/private registration identity on a short schedule and after any
suspected exposure. Monthly, rebuild from newly verified artifacts/base digests in staging, rerun
all isolation and egress tests, then atomically replace `images.env`. Retain the prior digest for
rollback; never roll back credentials. Remove the runner registration, containers, volumes,
networks, proxy, image digests, and GitHub App authorization to uninstall.

The remaining site-specific inputs are an organization-owned repository; a non-default group
restricted to exactly that repository and trusted workflow; the `ic-dev` Docker/AppArmor/iptables
host; repository and runner-group IDs; the repository's `all_external_contributors` policy; a
least-privilege organization-installed runner GitHub App ID/installation/new host-encrypted PEM; a
distinct status-only App ID/installation/private key in the protected `trusted-status-publisher`
environment; and live acceptance/ruleset evidence. The present personal repository is therefore a
hard activation blocker. No existing interactive GitHub CLI/OAuth token is an acceptable substitute.

For rollback, disable the timer, let an active job finish (or cancel it and remove its stale runner
registration), stop the runner and proxy, restore the previously accepted content image IDs, rerun
proxy and egress validation, then re-enable the timer. `uninstall-host.sh` stops services, removes
the proxy/firewall/network lifecycle, units, helpers, and AppArmor policies while deliberately
preserving `/etc/solar-ci-runner` and encrypted credentials for explicit recovery or secure erasure.

## One-time workflow bootstrap

`pull_request_target` always loads its workflow from the base branch, so the pull request that first
introduces `.github/workflows/trusted-ci.yml` cannot produce the new `intake` and `quality` statuses.
For that reason, the existing `.github/workflows/ci.yml` preserves its job names and four required
check contexts as the temporary hosted transition workflow; only its duplicate `push` trigger is
removed. Each legacy job checks out the event's exact head without persisting credentials and
asserts `git rev-parse HEAD`. Do not weaken those four previously required legacy checks. Merge the
first transition only when those exact-head checks and Product, Architecture, Tester, Security, and
Reviewer approval all pass.

During this one transition, the legacy `security` job runs exact-version Bandit and pip-audit tools
outside the candidate environment plus the full-SHA-pinned gitleaks action. It validates/exports the
lock without installing the project. The two matrix `test` jobs declare both `security` and
`dependency-review` as prerequisites, so no candidate install, test, build backend, or build starts
before both trusted evidence jobs succeed.

After the trusted workflow is on protected `main`, set and verify `all_external_contributors`, create
the two separate Apps and protected publisher environment, install and run host acceptance, then
enable the timer. Open a second same-repository PR from fresh `main` that deletes only legacy
`ci.yml`.
Because both workflows are still in the base, that PR must pass the legacy checks, the exact-head
`intake` and `quality` statuses, and the normal human gates. Also open a fork test PR and verify the
new statuses fail or remain absent and no runner is allocated. Add `intake` and `quality` to branch
protection while retaining the old contexts, merge the deletion PR while both workflow files and
both rule sets coexist, and only then remove the old required contexts. Record the ruleset audit
evidence. Steady state has only `trusted-ci.yml`: one local build and one tiny hosted publisher.

Tag-only release build/attestation stays on GitHub-hosted runners. Deployment verification uses
`--deny-self-hosted-runners`, so release jobs must never move to `ic-dev`.
