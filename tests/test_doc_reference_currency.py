# Anti-dangle guard for the documentation rename.
#
# The open-source cleanup renamed yaha/docs/agentclass.md to
# yaha/docs/agentclass-authoring-guide.md.  Any shipped source that still
# cites the OLD name is a dead reference, so this guard walks the shipped
# trees and fails if the literal old path reappears.
#
# Scope: yaha/ and examples/ only.  tests/ is deliberately excluded - the two
# remaining mentions there are explanatory comments, not live references.

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("yaha", "examples")
OLD_REF = "docs/agentclass.md"
SKIP_DIRS = {"__pycache__", ".git"}


def _shipped_files():
    for name in SCAN_DIRS:
        base = ROOT / name
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if fname.endswith(".md") or fname.endswith(".py"):
                    yield Path(dirpath) / fname


def test_no_shipped_source_cites_the_removed_agentclass_doc():
    offenders = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if OLD_REF in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "shipped sources cite the removed %s: %s" % (OLD_REF, offenders)