ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG UBUNTU_SNAPSHOT
RUN test -n "$UBUNTU_SNAPSHOT" \
    && sed -i "s|http://archive.ubuntu.com/ubuntu/|https://snapshot.ubuntu.com/ubuntu/$UBUNTU_SNAPSHOT/|g; s|http://security.ubuntu.com/ubuntu/|https://snapshot.ubuntu.com/ubuntu/$UBUNTU_SNAPSHOT/|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libicu74 libkrb5-3 liblttng-ust1 libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

ARG ACTIONS_RUNNER_SHA256
ARG UV_SHA256
ARG GH_SHA256
ARG GITLEAKS_SHA256
ARG PYTHON311_SHA256
ARG PYTHON312_SHA256

COPY artifacts/actions-runner.tar.gz /tmp/actions-runner.tar.gz
COPY artifacts/uv.tar.gz /tmp/uv.tar.gz
COPY artifacts/gh.tar.gz /tmp/gh.tar.gz
COPY artifacts/gitleaks.tar.gz /tmp/gitleaks.tar.gz
COPY artifacts/python311.tar.gz /tmp/python311.tar.gz
COPY artifacts/python312.tar.gz /tmp/python312.tar.gz
RUN echo "$ACTIONS_RUNNER_SHA256  /tmp/actions-runner.tar.gz" | sha256sum --check --strict \
    && echo "$UV_SHA256  /tmp/uv.tar.gz" | sha256sum --check --strict \
    && echo "$GH_SHA256  /tmp/gh.tar.gz" | sha256sum --check --strict \
    && echo "$GITLEAKS_SHA256  /tmp/gitleaks.tar.gz" | sha256sum --check --strict \
    && echo "$PYTHON311_SHA256  /tmp/python311.tar.gz" | sha256sum --check --strict \
    && echo "$PYTHON312_SHA256  /tmp/python312.tar.gz" | sha256sum --check --strict \
    && mkdir -p /opt/actions-runner /opt/python/3.11 /opt/python/3.12 /tmp/uv /tmp/gh /tmp/gitleaks \
    && tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner \
    && tar -xzf /tmp/uv.tar.gz -C /tmp/uv \
    && tar -xzf /tmp/gh.tar.gz -C /tmp/gh \
    && tar -xzf /tmp/gitleaks.tar.gz -C /tmp/gitleaks \
    && tar -xzf /tmp/python311.tar.gz --strip-components=1 -C /opt/python/3.11 \
    && tar -xzf /tmp/python312.tar.gz --strip-components=1 -C /opt/python/3.12 \
    && install -m 0555 "$(find /tmp/uv -type f -name uv -print -quit)" /usr/local/bin/uv \
    && install -m 0555 "$(find /tmp/gh -type f -name gh -print -quit)" /usr/local/bin/gh \
    && install -m 0555 "$(find /tmp/gitleaks -type f -name gitleaks -print -quit)" /usr/local/bin/gitleaks \
    && ln -s /opt/python/3.11/bin/python3.11 /usr/local/bin/python3.11 \
    && ln -s /opt/python/3.12/bin/python3.12 /usr/local/bin/python3.12 \
    && rm -rf /tmp/actions-runner.tar.gz /tmp/uv /tmp/uv.tar.gz /tmp/gh /tmp/gh.tar.gz /tmp/gitleaks /tmp/gitleaks.tar.gz /tmp/python311.tar.gz /tmp/python312.tar.gz \
    && useradd --uid 10001 --user-group --home-dir /home/runner --shell /usr/sbin/nologin runner

COPY pyproject.toml uv.lock README.md /tmp/project/
RUN UV_CACHE_DIR=/opt/uv-cache-seed uv sync --directory /tmp/project --frozen --all-extras \
      --no-install-project --python /opt/python/3.11/bin/python3.11 \
    && UV_CACHE_DIR=/opt/uv-cache-seed uv sync --directory /tmp/project --frozen --all-extras \
      --no-install-project --python /opt/python/3.12/bin/python3.12 \
    && rm -rf /tmp/project \
    && chown -R 10001:10001 /opt/actions-runner \
    && command -v uv >/dev/null && command -v gh >/dev/null && command -v gitleaks >/dev/null

COPY deployment/ci-runner/entrypoint.sh /usr/local/bin/solar-ci-entrypoint
COPY deployment/ci-runner/acceptance-probe.py /usr/local/libexec/solar-ci-acceptance-probe.py
RUN chmod 0555 /usr/local/bin/solar-ci-entrypoint \
    /usr/local/libexec/solar-ci-acceptance-probe.py

ENV UV_CACHE_DIR=/opt/uv-cache UV_OFFLINE=1
USER 10001:10001
WORKDIR /opt/actions-runner
ENTRYPOINT ["/usr/local/bin/solar-ci-entrypoint"]
