# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Regression guards for the complete removal of the task.md subsystem.

The per-class `.yaha/memory/<class>/task.md` task history was removed because a
file that gains one section per round is the wrong shape for an unbounded
multi-round agent.  These tests fail loudly if any part of it comes back.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import yaha.cli as cli
from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORBIDDEN_TOKENS = (
    "taskstore",
    "task_file_path",
    "recall_task_history",
    "_active_round_header",
)

# Shipped sources that must be free of every task.md trace.  Only tracked
# artifacts are scanned: `.yaha/memory` and `.yaha/logs` are runtime output and
# may legitimately quote anything.  The tracked `.yaha/` workspace config is
# no longer shipped in this repo (removed in the open-source cleanup).
SCAN_TREES = (os.path.join(REPO_ROOT, "yaha"),)
SCAN_FILES = (
    os.path.join(REPO_ROOT, "README.md"),
)

AGENT_GONE = (
    "_task_agent_class",
    "_record_preamble_round",
    "_close_round_on_boundary",
    "_close_session_rounds",
    "recall_task_history_impl",
)

AGENTCLASS_FIXTURE = """
## BaseAgent
- **Inherited:** None
- **Code:** Baa
- **System Prompt:**
    > worker system prompt
- **Preamble:**
    > worker preamble
- **Aftermath:**
    > worker done
"""


class TestTaskstoreRemoved(unittest.TestCase):
    def test_taskstore_module_is_not_importable(self):
        self.assertIsNone(importlib.util.find_spec("yaha.core.taskstore"))

    def test_no_token_survives_in_shipped_sources(self):
        hits = []
        for root in SCAN_TREES:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in filenames:
                    if not (name.endswith(".py") or name.endswith(".md")):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        text = open(path, encoding="utf-8").read()
                    except (OSError, UnicodeDecodeError):
                        continue
                    for token in FORBIDDEN_TOKENS:
                        if token in text:
                            hits.append("%s contains %s" % (path, token))
        for path in SCAN_FILES:
            text = open(path, encoding="utf-8").read()
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    hits.append("%s contains %s" % (path, token))
        self.assertEqual(hits, [])

    def test_agent_no_longer_carries_the_task_hooks(self):
        for name in AGENT_GONE:
            self.assertFalse(hasattr(Agent, name), name)
        self.assertNotIn("recall_task_history", dir(Agent))


class TestNoStartupTaskHistory(unittest.TestCase):
    """A leftover task.md on disk must not reach the startup prompt."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="yaha_no_taskmd_prompt_")
        yaha_dir = os.path.join(self.ws, ".yaha")
        for sub in ("memory", "ext", "logs", "skills"):
            os.makedirs(os.path.join(yaha_dir, sub), exist_ok=True)
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write(AGENTCLASS_FIXTURE)
        with open(os.path.join(yaha_dir, "task.md"), "w", encoding="utf-8") as f:
            f.write("""## legacy-sess
- **resumed from ** None
- **task prompt:** legacy secret work
- **status:** finished
""")
        self.engine = YAHAEngine(workspace_dir=self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_startup_prompt_ignores_a_leftover_task_file(self):
        agent = Agent(
            agent_config=self.engine.agent_classes["BaseAgent"],
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )
        prompt = agent.build_system_prompt()
        self.assertNotIn("TASK HISTORY", prompt)
        self.assertNotIn("legacy secret work", prompt)


class TestCliTargetsRemoved(unittest.TestCase):
    def test_list_tasks_mode_is_rejected(self):
        with patch("sys.argv", ["run-yaha", "--list", "tasks"]):
            with self.assertRaises(SystemExit):
                cli.main()

    def test_clear_flag_is_rejected(self):
        with patch("sys.argv", ["run-yaha", "-c"]):
            with self.assertRaises(SystemExit):
                cli.main()

    def test_help_output_mentions_neither_clear_nor_tasks(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with patch("sys.argv", ["run-yaha", "-h"]):
                with self.assertRaises(SystemExit):
                    cli.main()
        help_text = buffer.getvalue()
        self.assertNotIn("--clear", help_text)
        self.assertNotIn("tasks", help_text)
        self.assertIn("--list", help_text)


class TestCliRunWritesNoTaskFile(unittest.TestCase):
    """A full (mocked-engine) CLI run must leave no task.md behind."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_no_taskmd_cli_")
        yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(yaha_dir, "memory"))
        os.makedirs(os.path.join(yaha_dir, "ext"))
        os.makedirs(os.path.join(yaha_dir, "logs"))
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write(AGENTCLASS_FIXTURE)
        self.original_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("yaha.cli.YAHAEngine")
    @patch("sys.stdin.isatty", return_value=True)
    def test_cli_run_creates_no_task_md(self, mock_isatty, mock_engine_class):
        mock_engine = MagicMock()
        mock_engine_class.return_value = mock_engine
        mock_engine.run_agent.return_value = "Expected Agent Response Text"
        mock_engine.session.session_id = "test_session_id"
        mock_engine.session.folder_name = "test_session_folder"
        mock_engine.get_token_usage_report.return_value = "Tokens: 0"

        out_file = os.path.join(self.workspace, "out.txt")
        argv = [
            "run-yaha",
            "-w", self.workspace,
            "-a", "BaseAgent",
            "-o", out_file,
            "Do a task",
        ]
        with patch("sys.argv", argv):
            cli.main()

        self.assertTrue(os.path.exists(out_file))
        found = []
        for dirpath, _dirnames, filenames in os.walk(self.workspace):
            for name in filenames:
                if name == "task.md":
                    found.append(os.path.join(dirpath, name))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()