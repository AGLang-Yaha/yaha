import os
import shutil
import tempfile
import unittest
from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent
from yaha.config.agents import AgentClassConfig

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

class TestLoopTool(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_test_")
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

    def test_max_loops_default_unlimited_and_parsing(self):
        # Default: no Max Loops declared -> -1 (unlimited)
        agent_config = self.engine.agent_classes.get("BaseAgent")
        self.assertEqual(agent_config.max_loops, -1)

        from yaha.config.agents import parse_agent_class_md
        path = os.path.join(self.workspace, "AgentClassLoops.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("## Loopy\n- **Code:** Loo\n- **Model:** DefaultModel\n- **Max Loops:** 3\n- **System Prompt:** > sys\n")
        classes = parse_agent_class_md(path)
        self.assertEqual(classes["Loopy"].max_loops, 3)

    def test_max_loops_enforced_when_positive(self):
        # BaseAgent with an explicit loop cap of 1
        agent_config = self.engine.agent_classes.get("BaseAgent")
        agent_config.max_loops = 1

        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "loop_tool", "args": {"summary": "round 1"}}</tool_code>',
            '<tool_code>{"tool": "loop_tool", "args": {"summary": "round 2"}}</tool_code>',
            'should not reach the loop cap error',
        ]
        agent.llm = mock_llm

        result = agent.run("task 1")
        self.assertEqual(result, "Error: Maximum chain loops reached (1).")
        self.assertEqual(agent.loop_count, 2)

    def test_loop_tool_execution(self):
        # Retrieve parsed Agent config
        agent_config = self.engine.agent_classes.get("BaseAgent")
        self.assertIsNotNone(agent_config)
        
        # Instantiate Agent
        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        
        # Create a Mock LLM Provider
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "loop_tool", "args": {"summary": "first round done", "modifier": "second task instruction"}}</tool_code>',
            'Completed second task successfully.'
        ]
        agent.llm = mock_llm
        
        # Execute run
        result = agent.run("first task instruction")
        
        # Assertions
        self.assertEqual(result, "Completed second task successfully.")
        self.assertEqual(agent.loop_count, 1)
        self.assertEqual(mock_llm.calls, 2)
        
        # Check that history has preserved context and the loop_tool call/result
        history_contents = [msg["content"] for msg in agent.history]
        
        # The history should contain the tool results
        self.assertTrue(any("loop_tool" in str(content) for content in history_contents))
        # The modifier is injected as a one-shot [TASK MODIFIER] section on the
        # re-entered stage's prompt.
        self.assertTrue(any("[TASK MODIFIER]: second task instruction" in str(content) for content in history_contents))

    def test_loop_modifier_is_one_shot(self):
        # The modifier rides ONE re-entry only: a stage that loops twice
        # carries the modifier on the first re-entry and not on the second.
        agent_config = self.engine.agent_classes.get("BaseAgent")
        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "loop_tool", "args": {"modifier": "once only"}}</tool_code>',
            '<tool_code>{"tool": "loop_tool", "args": {}}</tool_code>',
            'done',
        ]
        agent.llm = mock_llm

        result = agent.run("task")
        self.assertEqual(result, "done")
        # Only the re-entered stage PROMPT carries the modifier - the system
        # prompt legitimately mentions the [TASK MODIFIER] mechanism itself
        # (the loop_tool schema documents it), so count user messages with
        # the exact injected form.
        modifier_prompts = [p for p in agent.history
                            if p.get("role") == "user"
                            and "[TASK MODIFIER]: once only" in str(p.get("content") or "")]
        self.assertEqual(len(modifier_prompts), 1)

    def test_loop_reenters_same_stage_without_advancing(self):
        # A two-stage class: Preamble loops, so Second is never reached while
        # the loop continues; when Preamble finally completes, Second runs.
        from yaha.config.agents import parse_agent_class_md
        path = os.path.join(self.workspace, "AgentClassTwo.md")
        with open(path, "w", encoding="utf-8") as f:
            # The toolset matters: without it the one-pass optimization treats
            # every reply (a loop_tool call included) as plain final text.
            f.write("## Two\n- **Code:** Two\n- **Model:** DefaultModel\n"
                    "- **System Prompt:** > sys\n"
                    "- **Preamble:**\n    > first\n"
                    "- **Second:**\n    > second\n"
                    "### Toolset\n\t- loop_tool\n")
        classes = parse_agent_class_md(path)
        agent = Agent(
            agent_config=classes["Two"],
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "loop_tool", "args": {"modifier": "again"}}</tool_code>',
            'preamble final',
            'second final',
        ]
        agent.llm = mock_llm

        result = agent.run("task")
        self.assertEqual(result, "second final")
        self.assertEqual(agent.loop_count, 1)
        # The looped Preamble ran twice (2 calls), then Second once.
        self.assertEqual(mock_llm.calls, 3)

    def test_non_dict_args_is_a_recoverable_malformed_call(self):
        # A model (seen in the wild from DeepSeek) may emit args as a JSON
        # ARRAY. That must come back as a malformed-call error inside the
        # stage - not as an AttributeError that kills the whole run.
        agent_config = self.engine.agent_classes.get("BaseAgent")
        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        mock_llm = MockLLMProvider()
        mock_llm.responses = [
            '<tool_code>{"tool": "loop_tool", "args": ["not", "a", "dict"]}</tool_code>',
            '<tool_code>{"tool": "loop_tool", "args": {"modifier": "m"}}</tool_code>',
            'done',
        ]
        agent.llm = mock_llm

        result = agent.run("task")
        self.assertEqual(result, "done")
        history = "\n".join(str(m.get("content", "")) for m in agent.history)
        self.assertIn("must be a JSON object", history)

    def test_structured_parser_self_reference(self):
        agent_config = self.engine.agent_classes.get("BaseAgent")
        agent = Agent(
            agent_config=agent_config,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller
        )
        
        # Nested </tool_code> tag inside a JSON string argument
        response = '<tool_code>{"tool": "create_file", "args": {"path": "rules.txt", "content": "You must end with </tool_code> tag."}}</tool_code>'
        
        parsed, error = agent.validate_response(response)
        
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "create_file")
        self.assertEqual(parsed["args"]["content"], "You must end with </tool_code> tag.")

    def test_log_full_history_turn_reconstruction(self):
        # Create a mock message history containing a tool result user message
        history = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "First prompt"},
            {"role": "assistant", "content": "I am thinking..."},
            {"role": "user", "content": "TOOL RESULT (some_tool):\n- Content: success"}
        ]
        
        from yaha.core.logger import AgentLogger
        agent_logger = AgentLogger(self.engine.session, "BaseAgent", "test_log_id")
        agent_logger.log_full_history(history)
        
        # Read the generated log file
        with open(agent_logger.log_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # The reconstructed file should contain exactly "## Turn 1"
        self.assertIn("## Turn 1", content)
        # It should NOT contain "## Turn 2" because the only other user message was a TOOL RESULT
        self.assertNotIn("## Turn 2", content)

if __name__ == "__main__":
    unittest.main()
