#!/usr/bin/env python3
"""Validate the current build and stable release as separate version sources."""

import json
from pathlib import Path
import re

from build import VERSION

PACKAGE = Path(__file__).resolve().parents[1]
PROJECT = PACKAGE.parents[1]
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+"


def release_number(version):
    return tuple(map(int, re.split(r"[.-]", version)))


def stable_version(project):
    stable = []
    for path in (project / "docs/releases").glob("v*.md"):
        match = re.fullmatch(rf"v({VERSION_PATTERN})", path.stem)
        if match and "<!-- release-status: stable -->" in path.read_text():
            stable.append(match[1])
    if not stable:
        raise ValueError("stable release: no release record marked stable")
    return max(stable, key=release_number)


def check(project=PROJECT, package=PACKAGE, build_version=VERSION):
    data = json.loads((package / "root/usr/share/routepilot/version.json").read_text())
    changelog = (project / "CHANGELOG.md").read_text()
    latest = re.search(rf"^## ({VERSION_PATTERN})\b", changelog, re.M)
    if data != {"product": build_version.rsplit("-", 1)[0], "package": build_version}:
        raise ValueError(f"build version: source version.json must match {build_version}")
    if not latest or latest[1] != build_version:
        raise ValueError(f"build version: newest CHANGELOG heading must match {build_version}")

    headings = re.findall(rf"^## ({VERSION_PATTERN})\b(.*)$", changelog, re.M)
    labels = {
        version: re.search(r"（(正式|预发布|本地测试版)）$", title) for version, title in headings
    }
    if any(not label for label in labels.values()):
        raise ValueError("CHANGELOG version headings must declare the release status")
    stable = stable_version(project)
    if stable in labels and labels[stable][1] != "正式":
        raise ValueError(f"stable release: CHANGELOG must mark {stable} as stable")
    if release_number(stable) > release_number(build_version):
        raise ValueError(f"stable release: {stable} exceeds current build {build_version}")
    readme = (project / "README.md").read_text()
    install = (package / "INSTALL.zh-CN.md").read_text()
    stable_refs = [
        rf"(?:最新|当前)正式版(?:为\s*)?\s*\*{{0,2}}v({VERSION_PATTERN})",
        rf"v({VERSION_PATTERN})\s+is the current stable plugin release",
        rf"\[\*{{0,2}}下载 v({VERSION_PATTERN}) 正式版",
        rf"routepilot-lab/imok/releases/tag/v({VERSION_PATTERN})",
        rf"(?:imOK-|luci-app-routepilot_)({VERSION_PATTERN})(?:-install-kit|-public-source|_all)",
    ]
    for name, body in (("README.md", readme), ("INSTALL.zh-CN.md", install)):
        versions = {version for pattern in stable_refs for version in re.findall(pattern, body)}
        if name == "README.md" and not versions:
            raise ValueError("stable release: README.md has no explicit stable version references")
        if versions - {stable}:
            raise ValueError(
                f"stable release: {name} references {sorted(versions)}; expected {stable}"
            )
    return {"build_version": build_version, "stable_version": stable}


if __name__ == "__main__":
    try:
        print(json.dumps(check()))
    except ValueError as error:
        raise SystemExit(str(error)) from error
