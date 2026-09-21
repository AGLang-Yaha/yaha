# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

# Guards that every shipped source file carries the project license header.
#
# The repository is Apache-2.0 (see LICENSE).  These tests keep the headers and
# the packaging metadata from drifting away from that single source of truth.

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SPDX_LINE = "SPDX-License-Identifier: Apache-2.0"

SCAN_ROOTS = ("yaha", "tests", "examples", ".yaha")
EXTRA_FILES = ("run-yaha",)
CODE_SUFFIXES = (".py", ".sh", ".js")
SKIP_PATH_PARTS = {"__pycache__", "show-hidden-files"}
# Runtime output that lives under `.yaha/` but is NOT shipped: `.gitignore`
# excludes it, so it can hold arbitrarily many files and directories (a
# `.yaha/logs/` tree can be thousands of directories deep).  Scanning it would
# make this guard depend on local runtime state instead of the repository.  The
# SHIPPED `.yaha/shellcopilot/` and `.yaha/skills/` stay in scope.
SKIP_REL_PREFIXES = (".yaha/logs", ".yaha/memory")


def _is_skipped_rel(rel_dir):
    return any(
        rel_dir == prefix or rel_dir.startswith(prefix + os.sep)
        for prefix in SKIP_REL_PREFIXES
    )


def _in_scope_files():
    for name in SCAN_ROOTS:
        base = ROOT / name
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, ROOT)
            dirnames[:] = [
                d
                for d in dirnames
                if d not in SKIP_PATH_PARTS
                and not _is_skipped_rel(os.path.join(rel_dir, d))
            ]
            for fname in filenames:
                if fname.endswith(CODE_SUFFIXES):
                    yield Path(dirpath) / fname
    for name in EXTRA_FILES:
        candidate = ROOT / name
        if candidate.is_file():
            yield candidate


def test_all_source_files_carry_the_apache_spdx_header():
    missing = []
    misplaced = []
    for path in _in_scope_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(ROOT))
        if SPDX_LINE not in text:
            missing.append(rel)
            continue
        if not any(SPDX_LINE in line for line in text.splitlines()[:3]):
            misplaced.append(rel)
    assert not missing, "source files missing the license header: %s" % sorted(missing)
    assert not misplaced, "license header not near the top: %s" % sorted(misplaced)


def test_license_metadata_agrees_with_the_license_file():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text, "LICENSE should be the Apache License"
    assert "Version 2.0" in license_text, "LICENSE should be version 2.0"

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = 'license = { text = "Apache-2.0" }'
    assert expected in pyproject, "pyproject.toml should declare Apache-2.0"
    assert not re.search(r"\bMIT\b", pyproject), "pyproject.toml still advertises MIT"
