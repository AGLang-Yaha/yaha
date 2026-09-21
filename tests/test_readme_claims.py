# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

# Code-grounded guards for README.md.
#
# The README is only trustworthy while it agrees with the repository and the
# CLI, so these tests check the CODE:
#   1. every relative Markdown link resolves,
#   2. every backticked repo path exists,
#   3. every long CLI flag the README teaches exists in yaha/cli.py,
#   4. no stale/removed claim survives and the canonical clone URL is present.

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CLI = ROOT / "yaha" / "cli.py"

CANONICAL_URL = "https://github.com/AGLang-Yaha/yaha.git"

# Strings that a previous cleanup disproved; none may reappear.
STALE_STRINGS = ("sora-city", "AgentBuilder", "agents/eval", "yaha-design.md")

# Long CLI flags YAHA documents; yaha/cli.py is the ground truth.
TAUGHT_FLAGS = (
    "--version",
    "--agent",
    "--file",
    "--workspace",
    "--include",
    "--sk-dir",
    "--init",
    "--upgrade",
    "--create_skill",
    "--list",
    "--compile",
    "--verbose",
    "--output",
    "--yes",
    "--force",
)

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_REL_PATH_RE = re.compile(r"`((?:yaha|tests|examples)/[\w./-]+\.(?:py|md))`")


def _readme():
    return README.read_text(encoding="utf-8")


def test_readme_links_resolve():
    missing = []
    for target in _LINK_RE.findall(_readme()):
        t = target.strip()
        if t.startswith(("http://", "https://", "#", "mailto:")):
            continue
        t = t.split("#", 1)[0]
        if not t:
            continue
        if not (ROOT / t).exists():
            missing.append(target)
    assert not missing, "README links do not resolve: %s" % missing


def test_readme_paths_exist():
    missing = []
    for rel in sorted(set(_REL_PATH_RE.findall(_readme()))):
        if not (ROOT / rel).exists():
            missing.append(rel)
    assert not missing, "README cites missing paths: %s" % missing


def test_readme_flags_are_real():
    cli = CLI.read_text(encoding="utf-8")
    missing = [flag for flag in TAUGHT_FLAGS if flag not in cli]
    assert not missing, "README teaches flags absent from yaha/cli.py: %s" % missing


def test_readme_has_no_stale_repos_or_removed_sections():
    text = _readme()
    for stale in STALE_STRINGS:
        assert stale not in text, "README still mentions %r" % stale
    assert CANONICAL_URL in text, "README must publish the canonical clone URL"


def test_readme_reflects_white_paper_concepts():
    paper = (ROOT / "yaha" / "docs" / "AOP-White-Paper.md").read_text(encoding="utf-8")
    headings = [h.strip() for h in re.findall(r"^##\s+(.*)$", paper, flags=re.MULTILINE)]
    concepts = []
    for heading in headings:
        if heading.lower() in ("introduction", "summary"):
            continue
        for token in re.split(r"\band\b", heading):
            token = token.strip().lower()
            if token:
                concepts.append(token)
    readme = _readme().lower()
    missing = [c for c in concepts if c not in readme]
    assert not missing, "README does not mention white-paper concepts: %s" % missing


def test_readme_documents_companion_tools():
    text = _readme().lower()
    assert "obsidian" in text, "README should document the Obsidian plugin"
    assert "shell-copilot" in text or "copilot-hook.sh" in text, "README should document the shell hook"