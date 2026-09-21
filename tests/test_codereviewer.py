#!/usr/bin/env python3
"""Regression tests for the CodeReviewer agent class in AgentClass.md.

The CodeReviewer class is a separate, long-running, read-only patch reviewer:

    Preamble(Load Patch) -> Analyze -> Review -> Verify Tests -> Report
        -> Query_user -> Aftermath

It must remain read-only on the code under review (only the Report stage may
write files) and it must verify the patch's tests before reporting results.

This file lives in worktree/yaha/tests so patchman exports it as canonical
<repo>/tests/test_codereviewer.py; an identical copy lives in selfcoding/tests
where the local reftest runner executes it.
"""
from pathlib import Path
import unittest

import yaha
from yaha.config.agents import parse_agent_class_md

# Resolve the template from the imported package so this test works both under
# the selfcoding worktree and in the canonical checkout.
TEMPLATE = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"

EXPECTED_CHAIN = [
    "Preamble",
    "Analyze",
    "Review",
    "Verify Tests",
    "Report",
    "Query_user",
    "Aftermath",
]


def _classes():
    assert TEMPLATE.exists(), f"AgentClass.md not found at {TEMPLATE}"
    return parse_agent_class_md(str(TEMPLATE))


def _reviewer():
    cr = _classes().get("CodeReviewer")
    assert cr is not None, "CodeReviewer missing from AgentClass.md"
    return cr


def _stage(cr, name):
    return next(s for s in cr.prompt_chain if s["name"] == name)


def test_codereviewer_class_structure():
    cr = _reviewer()
    assert cr.code == "Cvr", cr.code
    assert cr.inherited == "BaseReflector", cr.inherited
    # Long-running agents run without a chain-operation cap (like ChatAgent).
    assert cr.max_loops == -1, cr.max_loops
    chain = [s["name"] for s in cr.prompt_chain]
    assert chain == EXPECTED_CHAIN, chain
    print("test_codereviewer_class_structure OK")


def test_codereviewer_preamble_loads_patch():
    cr = _reviewer()
    pre = _stage(cr, "Preamble")
    assert "read_file" in pre["required_tools"], pre["required_tools"]
    assert "success_tool" in pre["exit_tools"], pre["exit_tools"]
    assert "fail_tool" in pre["exit_tools"], pre["exit_tools"]
    # A patch that cannot be loaded must ask for another patch, not end the run.
    assert pre["fail_target"] == "Query_user", pre["fail_target"]
    assert "patch" in pre["content"].lower(), pre["content"]
    print("test_codereviewer_preamble_loads_patch OK")


def test_codereviewer_review_is_read_only():
    cr = _reviewer()
    rev = _stage(cr, "Review")
    # The review stage must not be able to modify the code under review.
    for writer in ("write_file", "edit_file", "create_file", "append_file"):
        assert writer not in rev["allowed_tools"], (writer, rev["allowed_tools"])
    for sev in ("BLOCKER", "MAJOR", "MINOR", "NIT"):
        assert sev in rev["content"], sev
    print("test_codereviewer_review_is_read_only OK")


def test_codereviewer_verify_tests_runs_command():
    cr = _reviewer()
    vt = _stage(cr, "Verify Tests")
    assert "bash_tool" in vt["required_tools"], vt["required_tools"]
    assert "bash_tool" in vt["allowed_tools"], vt["allowed_tools"]
    low = vt["content"].lower()
    assert "reftest" in low or "test command" in low or "pytest" in low, vt["content"]
    assert "exit code" in low, vt["content"]
    print("test_codereviewer_verify_tests_runs_command OK")


def test_codereviewer_report_writes_review_md():
    cr = _reviewer()
    rep = _stage(cr, "Report")
    assert "write_file" in rep["required_tools"], rep["required_tools"]
    assert "write_file" in rep["allowed_tools"], rep["allowed_tools"]
    assert ".review.md" in rep["content"], rep["content"]
    for sev in ("BLOCKER", "MAJOR", "MINOR", "NIT"):
        assert sev in rep["content"], sev
    assert "APPROVE" in rep["content"] and "REQUEST_CHANGES" in rep["content"]
    assert "Test verification" in rep["content"], rep["content"]
    print("test_codereviewer_report_writes_review_md OK")


def test_codereviewer_query_user_loops():
    cr = _reviewer()
    q = _stage(cr, "Query_user")
    assert set(q["allowed_tools"]) == {"ask_user", "exit_to", "success_tool"}, q["allowed_tools"]
    assert "ask_user" in q["required_tools"], q["required_tools"]
    assert set(q["exit_tools"]) == {"exit_to", "success_tool"}, q["exit_tools"]
    assert "exit_to" in q["content"], q["content"]
    print("test_codereviewer_query_user_loops OK")


def test_codereviewer_aftermath_keeps_memory_reflection():
    cr = _reviewer()
    am = _stage(cr, "Aftermath")
    assert "memory" in am["content"].lower(), am["content"]
    # Aftermath is restricted to file/memory tools, not code review edits.
    assert "read_file" in am["allowed_tools"], am["allowed_tools"]
    print("test_codereviewer_aftermath_keeps_memory_reflection OK")


def test_codereviewer_toolset_inheritance():
    cr = _reviewer()
    for t in ("read_file", "write_file", "bash_tool", "list_files",
              "search_files", "grep_files"):
        assert t in cr.toolset, (t, cr.toolset)
    print("test_codereviewer_toolset_inheritance OK")


class CodeReviewerTests(unittest.TestCase):
    """Unittest-discoverable wrapper for the regression functions above."""
    test_codereviewer_class_structure = staticmethod(test_codereviewer_class_structure)
    test_codereviewer_preamble_loads_patch = staticmethod(test_codereviewer_preamble_loads_patch)
    test_codereviewer_review_is_read_only = staticmethod(test_codereviewer_review_is_read_only)
    test_codereviewer_verify_tests_runs_command = staticmethod(test_codereviewer_verify_tests_runs_command)
    test_codereviewer_report_writes_review_md = staticmethod(test_codereviewer_report_writes_review_md)
    test_codereviewer_query_user_loops = staticmethod(test_codereviewer_query_user_loops)
    test_codereviewer_aftermath_keeps_memory_reflection = staticmethod(test_codereviewer_aftermath_keeps_memory_reflection)
    test_codereviewer_toolset_inheritance = staticmethod(test_codereviewer_toolset_inheritance)


if __name__ == "__main__":
    unittest.main()
