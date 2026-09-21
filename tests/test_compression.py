# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import shutil
import tempfile
import unittest
from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent
from yaha.config.models import ModelConfig, parse_model_md

class MockLLMProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []
        self.usage = {"prompt_tokens": 10, "completion_tokens": 20}

    def generate(self, messages, **kwargs):
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = "No response"
        self.calls += 1
        return resp, dict(self.usage), None

class TestContextCompression(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_test_compress_")
        yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(yaha_dir, "memory"))
        os.makedirs(os.path.join(yaha_dir, "ext"))
        os.makedirs(os.path.join(yaha_dir, "logs"))
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write("## BaseAgent\n- **Code:** Baa\n- **Model:** DefaultModel\n- **System Prompt:** > Base system prompt\n### Toolset\n\t- loop_tool\n")
        with open(os.path.join(yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")

        self.engine = YAHAEngine(workspace_dir=self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _make_agent(self):
        agent_config = self.engine.agent_classes.get("BaseAgent")
        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        return agent

    def test_model_md_context_size_parsing(self):
        model_md = os.path.join(self.workspace, "Model.md")
        with open(model_md, "w", encoding="utf-8") as f:
            f.write("## test_model\n- **provider:** ollama\n- **model:** x\n- **context_size:** 64000\n")
        models = parse_model_md(model_md)
        self.assertEqual(models["test_model"].context_size, 64000)
        # Missing context_size -> None (callers fall back to 128000)
        cfg = ModelConfig(name="m", provider="ollama", model="x")
        self.assertIsNone(cfg.context_size)

    def test_l1_truncates_old_tool_results(self):
        agent = self._make_agent()
        big_result = "TOOL RESULT (bash_tool):\n- **Status**: SUCCESS\n- **Content**:\n```\n" + ("x" * 5000) + "\n```"
        agent.history = [{"role": "system", "content": "sys"}]
        for _ in range(3):
            agent.history.append({"role": "assistant", "content": "calling tool"})
            agent.history.append({"role": "user", "content": big_result})
        # 6 recent messages kept intact; history length must not change
        agent.history.append({"role": "assistant", "content": "recent"})
        agent.history.append({"role": "user", "content": "TOOL RESULT (bash_tool): small recent"})

        n = agent._truncate_old_tool_results(keep_last=6, head_chars=500)

        self.assertEqual(n, 1)  # only the oldest result is outside the keep window
        self.assertEqual(len(agent.history), 9)
        self.assertTrue(agent.history[2]["content"].startswith("COMPRESSED TOOL RESULT (bash_tool):"))
        self.assertIn("truncated by context compression", agent.history[2]["content"])
        # Recent results untouched
        self.assertTrue(agent.history[4]["content"].startswith("TOOL RESULT ("))
        self.assertTrue(agent.history[6]["content"].startswith("TOOL RESULT ("))

    def test_l1_only_when_target_reached(self):
        agent = self._make_agent()
        agent.context_size = 1000  # 0.4 target = 400 tokens = 1600 chars
        big_result = "TOOL RESULT (bash_tool):\n- **Status**: SUCCESS\n- **Content**:\n```\n" + ("x" * 9000) + "\n```"
        agent.history = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": big_result},   # old, gets truncated
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": big_result},   # old, gets truncated
            # last 6 messages: small, inside the keep window
            {"role": "assistant", "content": "c"},
            {"role": "user", "content": "TOOL RESULT (bash_tool): small recent"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "TOOL RESULT (bash_tool): small recent 2"},
            {"role": "assistant", "content": "e"},
            {"role": "user", "content": "TOOL RESULT (bash_tool): small recent 3"},
        ]
        agent.logger.set_task("task")

        restarted, _ = agent._compress_context({"name": "Task"}, "stage prompt", 1)

        self.assertFalse(restarted)
        self.assertEqual(agent.compress_count, 1)
        self.assertTrue(agent.history[2]["content"].startswith("COMPRESSED TOOL RESULT"))
        self.assertTrue(agent.history[4]["content"].startswith("COMPRESSED TOOL RESULT"))
        # L1 happened, but no L2 digest was injected
        self.assertFalse(any("CONTEXT DIGEST" in (m.get("content") or "") for m in agent.history))
        with open(agent.logger.log_path, "r", encoding="utf-8") as f:
            log = f.read()
        self.assertIn("## Context Compressed", log)
        self.assertIn("L1 truncation", log)

    def test_l2_cold_restart_and_memory_index_reload(self):
        agent = self._make_agent()
        agent.context_size = 1000  # trigger at 800 tokens; target 400 tokens
        mock_llm = MockLLMProvider()
        # Real usage reports a nearly-full window after the first call
        mock_llm.usage = {"prompt_tokens": 900, "completion_tokens": 20}
        mock_llm.responses = [
            # 1st call: bulky thought + loop_tool so the chain continues and the
            # compression trigger fires at the top of the next stage execution
            ("thinking " * 350) + '<tool_code>{"tool": "loop_tool", "args": {"modifier": "second task"}}</tool_code>',
            # 2nd call: reflection digest (plain text ends the Compression stage)
            "GOAL: finish the task\nCOMPLETED: nothing yet\nPENDING: everything\nKEY FILES: none\nERRORS & LESSONS: none",
            # 3rd call: the real answer after restart
            "final answer after compression",
        ]
        agent.llm = mock_llm

        # A memory file whose frontmatter description must appear in the rebuilt system prompt
        with open(os.path.join(self.workspace, ".yaha", "memory", "notes.md"), "w", encoding="utf-8") as f:
            f.write("---\ndescription: compression test memory\n---\n\nfacts\n")

        result = agent.run("first task")

        self.assertEqual(result, "final answer after compression")
        self.assertEqual(agent.compress_count, 1)
        # History was rebuilt: system + digest + re-injected stage prompt (+ final answer)
        self.assertEqual(len(agent.history), 4)
        self.assertIn("CONTEXT DIGEST", agent.history[1]["content"])
        self.assertIn("GOAL: finish the task", agent.history[1]["content"])
        self.assertIn("CONTEXT RESTORED", agent.history[2]["content"])
        self.assertEqual(agent.history[3]["content"], "final answer after compression")
        # Memory index reloaded into the fresh system prompt
        self.assertIn("MEMORY INDEX", agent.history[0]["content"])
        self.assertIn("compression test memory", agent.history[0]["content"])
        # Log carries the compression marker
        with open(agent.logger.log_path, "r", encoding="utf-8") as f:
            log = f.read()
        self.assertIn("## Context Compressed", log)
        self.assertIn("L2 reflection + cold restart", log)

    def test_no_compression_below_threshold(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = ["plain answer"]
        agent.llm = mock_llm

        result = agent.run("small task")

        self.assertEqual(result, "plain answer")
        self.assertEqual(agent.compress_count, 0)
        self.assertEqual(mock_llm.calls, 1)

if __name__ == "__main__":
    unittest.main()
