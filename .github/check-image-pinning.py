# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Every container image is pinned by digest (C16).

An image referenced by tag is whatever the registry says today. Pinning by digest is what
makes "the thing we tested is the thing that runs" true, and it is what gives `cosign
verify` something fixed to verify against.

Two cases a grep gets wrong, which is why this is a script:

* **Locally built images.** A Compose service with a `build:` section has no digest to
  pin — it does not come from a registry. What matters there is its Dockerfile's `FROM`,
  which is checked on its own.
* **Multi-stage Dockerfiles.** `FROM builder AS final` names an earlier stage, not an
  image. Demanding a digest there is nonsense.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
FROM_LINE = re.compile(r"^\s*FROM\s+(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.IGNORECASE)

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


def compose_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [
        path
        for path in root.rglob("*.y*ml")
        if not SKIP_DIRS & set(path.parts) and path.name.startswith("docker-compose")
    ]


def dockerfiles(root: pathlib.Path) -> list[pathlib.Path]:
    return [
        path
        for path in root.rglob("Dockerfile*")
        if not SKIP_DIRS & set(path.parts) and path.is_file()
    ]


def check_compose(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    try:
        document = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as bad:
        return [f"{path}: could not be parsed — {bad}"]

    for name, service in (document.get("services") or {}).items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if service.get("build"):
            # Built here, so there is no registry digest to pin. Its Dockerfile is
            # checked separately.
            continue
        if image and not DIGEST.search(str(image)):
            problems.append(f"{path}: service {name!r} uses {image} — no digest")
    return problems


def check_dockerfile(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    stages: set[str] = set()
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        match = FROM_LINE.match(line)
        if not match:
            continue
        reference = match.group("ref")
        if match.group("stage"):
            stages.add(match.group("stage").lower())
        if reference.lower() in stages or reference.lower() == "scratch":
            continue
        if not DIGEST.search(reference):
            problems.append(f"{path}:{number}: FROM {reference} — no digest")
    return problems


def main() -> int:
    root = pathlib.Path().resolve()
    problems: list[str] = []
    checked = 0

    for path in compose_files(root):
        checked += 1
        problems += check_compose(path)
    for path in dockerfiles(root):
        checked += 1
        problems += check_dockerfile(path)

    if problems:
        print("image references that are not pinned by digest:")
        for problem in problems:
            print(f"  {problem}")
        print("\nResolve one with: docker buildx imagetools inspect <image>:<tag>")
        return 1

    print(f"all image references in {checked} file(s) are pinned by digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
