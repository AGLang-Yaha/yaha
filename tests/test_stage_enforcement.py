import os
import shutil
import tempfile
import unittest
from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent


class MockLLMProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []

    def generate(self, messages, **kwargs):
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = "No response"
        self.calls += 1
        return resp, {"prompt_tokens": 10, "completion_tokens": 20}, None


class TestStageEnforcement(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_test_")
        yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(yaha_dir, "memory"))
        os.makedirs(os.path.join(yaha_dir, "ext"))
        os.makedirs(os.path.join(yaha_dir, "logs"))
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write(
                "## BaseAgent\n"
                "- **Code:** Baa\n"
                "- **Model:** DefaultModel\n"
                "- **System Prompt:** > Base system prompt\n"
                "### Toolset\n"
                "\t- read_file\n"
                "\t- write_file\n"
                "\t- success_tool\n"
                "\t- fail_tool\n"
                "- **Preamble:**\n"
                "    > preamble prompt. Call read_file, then end this stage with success_tool.\n"
                "  - **Allowed Tools:**\n"
                "    - read_file\n"
                "  - **Required Tools:**\n"
                "    - read_file\n"
                "  - **Exit Tools:**\n"
                "    - success_tool\n"
                "    - fail_tool\n"
                "- **Task:**\n"
                "    > task prompt. End this stage with success_tool to advance, or fail_tool to route to Aftermath for failure handling.\n"
                "  - **Exit Tools:**\n"
                "    - success_tool\n"
                "    - fail_tool\n"
                "  - **Fail Target:** aftermath\n"
                "- **Aftermath:**\n"
                "    > aftermath prompt. End this stage with success_tool.\n"
            )
        with open(os.path.join(yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")

        self.engine = YAHAEngine(workspace_dir=self.workspace)
        self.agent_config = self.engine.agent_classes.get("BaseAgent")
        self.assertIsNotNone(self.agent_config)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _make_agent(self):
        return Agent(
            agent_config=self.agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )

    def test_success_tool_advances_when_requirements_met(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "preamble done"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "task done"}}</tool_code>',
            'aftermath summary',
        ]
        agent.llm = mock_llm

        # Create the file so read_file succeeds
        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "aftermath summary")
        self.assertEqual(mock_llm.calls, 4)

    def test_success_tool_rejected_when_requirements_missing(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "success_tool", "args": {"summary": "skip"}}</tool_code>',
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "preamble done"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "task done"}}</tool_code>',
            'aftermath summary',
        ]
        agent.llm = mock_llm

        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "aftermath summary")
        self.assertEqual(mock_llm.calls, 5)

    def test_plain_text_rejected_when_exit_tools_required(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            "I think I am done.",
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "preamble done"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "task done"}}</tool_code>',
            'aftermath summary',
        ]
        agent.llm = mock_llm

        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "aftermath summary")
        self.assertEqual(mock_llm.calls, 5)

    def test_fail_tool_jumps_to_target_stage(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "fail_tool", "args": {"reason": "skip to reflection"}}</tool_code>',
            'reflection done',
        ]
        agent.llm = mock_llm

        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "reflection done")
        self.assertEqual(mock_llm.calls, 3)

    def test_max_turns_minus_one_parsing(self):
        # `Max Turns` is RETIRED as an enforcement mechanism, but the loader
        # still parses it so an older AgentClass file loads without error.
        from yaha.config.agents import parse_agent_class_md
        path = os.path.join(self.workspace, "AgentClassNeg.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "## Chatty\n"
                "- **Code:** Cha\n"
                "- **Model:** DefaultModel\n"
                "- **Max Turns:** -1\n"
                "- **System Prompt:** > sys\n"
                "- **Preamble:**\n"
                "    > preamble prompt\n"
                "  - **Max Turns:** -1\n"
            )
        classes = parse_agent_class_md(path)
        self.assertEqual(classes["Chatty"].max_turns, -1)
        self.assertEqual(classes["Chatty"].prompt_chain[0]["max_turns"], -1)

    def _parse_chain_md(self, extra_classes):
        from yaha.config.agents import parse_agent_class_md
        path = os.path.join(self.workspace, "AgentClassInherit.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "## Parent\n"
                "- **Inherited:** None\n"
                "- **Code:** Par\n"
                "- **Model:** DefaultModel\n"
                "- **System Prompt:** > parent sys\n"
                "- **Preamble:**\n"
                "    > parent preamble\n"
                "  - **Final Answer:** True\n"
                "  - **Exit Tools:**\n"
                "    - success_tool\n"
                + extra_classes
            )
        return parse_agent_class_md(path)

    def test_stage_metadata_inherited_on_inherit_all(self):
        classes = self._parse_chain_md(
            "## Child\n"
            "- **Inherited:** Parent\n"
            "- **Code:** Chi\n"
            "- **Model:** DefaultModel\n"
            "- **System Prompt:** > child sys\n"
            "- **Preamble:**\n"
            "    - **Inherited:** All\n"
            "    > child preamble\n"
        )
        preamble = next(s for s in classes["Child"].prompt_chain if s["name"] == "Preamble")
        self.assertTrue(preamble["final_answer"])
        self.assertEqual(preamble["exit_tools"], ["success_tool"])
        self.assertIn("parent preamble", preamble["content"])
        self.assertIn("child preamble", preamble["content"])

    def test_stage_metadata_explicit_override_wins(self):
        classes = self._parse_chain_md(
            "## Child\n"
            "- **Inherited:** Parent\n"
            "- **Code:** Chi\n"
            "- **Model:** DefaultModel\n"
            "- **System Prompt:** > child sys\n"
            "- **Preamble:**\n"
            "    - **Inherited:** All\n"
            "    - **Final Answer:** False\n"
            "    > child preamble\n"
        )
        preamble = next(s for s in classes["Child"].prompt_chain if s["name"] == "Preamble")
        # Explicitly authored False must NOT be overridden by the parent's True
        self.assertFalse(preamble["final_answer"])
        # Fields not authored by the child are still inherited
        self.assertEqual(preamble["exit_tools"], ["success_tool"])

    def test_stage_metadata_inheritance_is_transitive(self):
        classes = self._parse_chain_md(
            "## Child\n"
            "- **Inherited:** Parent\n"
            "- **Code:** Chi\n"
            "- **Model:** DefaultModel\n"
            "- **System Prompt:** > child sys\n"
            "- **Preamble:**\n"
            "    - **Inherited:** All\n"
            "    > child preamble\n"
            "## GrandChild\n"
            "- **Inherited:** Child\n"
            "- **Code:** Grd\n"
            "- **Model:** DefaultModel\n"
            "- **System Prompt:** > grandchild sys\n"
            "- **Preamble:**\n"
            "    - **Inherited:** All\n"
            "    > grandchild preamble\n"
        )
        preamble = next(s for s in classes["GrandChild"].prompt_chain if s["name"] == "Preamble")
        # GrandChild inherits Child, which itself only inherited the field from Parent
        self.assertTrue(preamble["final_answer"])
        self.assertEqual(preamble["exit_tools"], ["success_tool"])

    def test_allowed_tools_blocks_disallowed_tool(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            # Disallowed tool call is rejected
            '<tool_code>{"tool": "write_file", "args": {"path": "x.txt", "content": "hello"}}</tool_code>',
            # Allowed tool call succeeds
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "preamble done"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "task done"}}</tool_code>',
            'aftermath summary',
        ]
        agent.llm = mock_llm

        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "aftermath summary")
        self.assertEqual(mock_llm.calls, 5)

    def test_final_answer_is_logged(self):
        """The response returned by run() must be present in the agent log."""
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "read_file", "args": {"path": "x.txt", "start_line": 0, "end_line": -1}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "preamble done"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "task done"}}</tool_code>',
            'aftermath summary',
        ]
        agent.llm = mock_llm

        with open(os.path.join(self.workspace, "x.txt"), "w") as f:
            f.write("hello")

        result = agent.run("do it")
        self.assertEqual(result, "aftermath summary")

        with open(agent.logger.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("aftermath summary", log_content)


class TestChatAgentQueryUser(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_chat_")
        yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(yaha_dir, "memory"))
        os.makedirs(os.path.join(yaha_dir, "ext"))
        os.makedirs(os.path.join(yaha_dir, "logs"))
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write(
                "## ChatAgent\n"
                "- **Code:** Cha\n"
                "- **Model:** DefaultModel\n"
                "- **System Prompt:** > Base system prompt\n"
                "### Toolset\n"
                "\t- ask_user\n"
                "\t- exit_to\n"
                "\t- success_tool\n"
                "- **Task:**\n"
                "    > task prompt\n"
                "- **Query_user:**\n"
                "    - **Required Tools:**\n"
                "      - ask_user\n"
                "    - **Exit Tools:**\n"
                "      - exit_to\n"
                "      - success_tool\n"
                "    > QUERY USER PROTOCOL:\n"
                "    > 1. Call ask_user.\n"
                "    > 2. If user wants to stop, call success_tool.\n"
                "    > 3. If user has a new task, call exit_to('Task').\n"
            )
        with open(os.path.join(yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")

        self.engine = YAHAEngine(workspace_dir=self.workspace)
        self.agent_config = self.engine.agent_classes.get("ChatAgent")
        self.assertIsNotNone(self.agent_config)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _make_agent(self):
        return Agent(
            agent_config=self.agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )

    def test_Query_user_requires_ask_user_before_jump(self):
        agent = self._make_agent()
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            # Task stage ends with text (no exit_tools)
            "task done",
            # Query_user: try to jump without asking -> rejected
            '<tool_code>{"tool": "exit_to", "args": {"name": "Task"}}</tool_code>',
            # ask the user
            '<tool_code>{"tool": "ask_user", "args": {"question": "What next?"}}</tool_code>',
            # now jump back to the chain head with the new task
            '<tool_code>{"tool": "exit_to", "args": {"name": "Task", "summary": "do more"}}</tool_code>',
            # second Task stage
            "task done again",
            # second Query_user: ask then goodbye
            '<tool_code>{"tool": "ask_user", "args": {"question": "What next?"}}</tool_code>',
            '<tool_code>{"tool": "success_tool", "args": {"summary": "goodbye"}}</tool_code>',
        ]
        agent.llm = mock_llm

        result = agent.run("do it")
        self.assertEqual(result, "goodbye")
        self.assertEqual(agent.loop_count, 1)
        self.assertEqual(mock_llm.calls, 7)


if __name__ == "__main__":
    unittest.main()
