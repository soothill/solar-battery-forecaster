from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

EXPECTED = {
    "actions-runner.tar.gz",
    "uv.tar.gz",
    "gh.tar.gz",
    "gitleaks.tar.gz",
    "python311.tar.gz",
    "python312.tar.gz",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
PYTHON_VERSION = re.compile(r"^3\.(11|12)\.[0-9]+$")
SNAPSHOT = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
MAX_ARTIFACT_BYTES = 500 * 1024 * 1024


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: fetch-artifacts.py TOOLCHAIN_JSON OUTPUT_DIRECTORY")
    manifest_path = Path(sys.argv[1])
    output = Path(sys.argv[2])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        IMAGE_DIGEST.fullmatch(str(manifest.get("runner_base_image"))) is None
        or IMAGE_DIGEST.fullmatch(str(manifest.get("proxy_base_image"))) is None
        or PYTHON_VERSION.fullmatch(str(manifest.get("python311_version"))) is None
        or PYTHON_VERSION.fullmatch(str(manifest.get("python312_version"))) is None
        or SNAPSHOT.fullmatch(str(manifest.get("ubuntu_snapshot"))) is None
    ):
        raise SystemExit("toolchain manifest has invalid pinned platform inputs")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != EXPECTED:
        raise SystemExit("toolchain manifest has an unexpected artifact set")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename in sorted(EXPECTED):
        record = artifacts[filename]
        if not isinstance(record, dict):
            raise SystemExit("toolchain artifact record is invalid")
        url = record.get("url")
        expected_hash = record.get("sha256")
        if (
            not isinstance(url, str)
            or urlparse(url).scheme != "https"
            or not isinstance(expected_hash, str)
            or SHA256.fullmatch(expected_hash) is None
        ):
            raise SystemExit("toolchain artifact URL or checksum is invalid")
        temporary = output / f".{filename}.partial"
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(url, headers={"User-Agent": "solar-ci-builder/1"})
        # The manifest accepts HTTPS only and every download is verified by SHA-256.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=60
        ) as response, temporary.open("wb") as handle:
            if urlparse(response.url).scheme != "https":
                raise SystemExit("toolchain artifact redirected away from HTTPS")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise SystemExit("toolchain artifact exceeded the size limit")
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != expected_hash:
            temporary.unlink(missing_ok=True)
            raise SystemExit("toolchain artifact checksum did not match")
        temporary.replace(output / filename)


if __name__ == "__main__":
    main()
