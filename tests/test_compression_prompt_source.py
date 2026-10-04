# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""The compression / Snap summary INSTRUCTIONS must come from the `Compression`
detached stage content (the single source of truth), never from a hard-coded
runtime prompt. The runtime injects only the dynamic values it owns (occupancy,
target, stage name, original task)."""

import unittest
from pathlib import Path

import yaha
from yaha.config.agents import parse_agent_class_md

from _compression_fixture import AgentCase


SENTINEL = "ZZ-SENTINEL-INSTRUCTIONS-ZZ"


class SentinelCase(AgentCase):
    """AgentCase whose Compression stage content is a unique sentinel body."""

    def __init__(self, context_size=400):
        super().__init__(context_size=context_size)
        comp = self.agent.config.detached_stages["Compression"]
        comp["content"] = SENTINEL + "\nUse EXACTLY these sections: GOAL / KEY LEARNINGS."


class TestSnapPromptSource(unittest.TestCase):
    def test_snap_prompt_is_stage_content_plus_dynamic_footer(self):
        with SentinelCase() as case:
            agent = case.agent
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'x' * 3000},
            ]
            agent._maybe_snap({'name': 'Preamble', 'snap': 'enabled'}, 1, force=True)
            prompt = agent.llm.sent[0][-1]['content']
            # The stage content leads; the runtime only appends the dynamic footer.
            self.assertTrue(prompt.startswith(SENTINEL))
            self.assertIn('[SNAP REQUEST]', prompt)
            self.assertIn("Stage 'Preamble' just completed.", prompt)
            self.assertIn('Context usage is', prompt)
            self.assertIn('target after snap', prompt)
            # The runtime authors NO instruction prose of its own.
            self.assertNotIn('using sections: GOAL,', prompt)

    def test_snap_prompt_does_not_reinject_hard_coded_section_spec(self):
        with AgentCase(context_size=400) as case:
            agent = case.agent
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'x' * 3000},
            ]
            agent._maybe_snap({'name': 'Preamble', 'snap': 'enabled'}, 1, force=True)
            prompt = agent.llm.sent[0][-1]['content']
            # The fixture's Compression body is the bare marker; if the runtime
            # still hard-coded the section list, the spec would appear anyway.
            self.assertIn('handler body', prompt)
            self.assertNotIn('DESIGN & ARCHITECTURE DECISIONS', prompt)


class TestReflectionPromptSource(unittest.TestCase):
    def test_reflection_prompt_is_stage_content_plus_dynamic_footer(self):
        with SentinelCase() as case:
            agent = case.agent
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'assistant', 'content': 'z' * 3000},
                {'role': 'user', 'content': 'w' * 3000},
            ]
            agent.logger.set_task('task')
            agent._compress_context({'name': 'Coding'}, 'STAGE PROMPT', 1)
            prompt = agent.llm.sent[0][-1]['content']
            self.assertTrue(prompt.startswith(SENTINEL))
            self.assertIn('[COMPRESSION REQUEST]', prompt)
            self.assertIn("You are in stage 'Coding'.", prompt)
            self.assertNotIn('using sections: GOAL,', prompt)

    def test_no_compression_stage_authors_no_prompt(self):
        with AgentCase(context_size=400) as case:
            agent = case.agent
            agent.config.detached_stages = {}
            agent.config.snap_handler = None
            agent.history = [
                {'role': 'system', 'content': 'sys'},
                {'role': 'assistant', 'content': 'z' * 3000},
                {'role': 'user', 'content': 'w' * 3000},
            ]
            before = agent.llm.calls
            digest, turn = agent._reflect_for_compression({'name': 'Coding'}, 1)
            self.assertIsNone(digest)
            self.assertIsInstance(turn, int)
            # No Compression stage -> the runtime fabricates no prompt, so no call.
            self.assertEqual(agent.llm.calls, before)


class TestTemplateSourceOfTruth(unittest.TestCase):
    def test_shipped_template_compression_stage_names_all_sections(self):
        template = Path(yaha.__file__).resolve().parent / "templates" / "AgentClass.md"
        classes = parse_agent_class_md(str(template))
        handler = classes["BaseAgent"].detached_stages.get("Compression")
        self.assertIsNotNone(handler)
        # The stage content wraps across lines, so compare on collapsed whitespace.
        content = " ".join((handler.get("content") or "").split())
        for section in ("GOAL", "KEY LEARNINGS", "DESIGN & ARCHITECTURE DECISIONS",
                        "PENDING", "KEY FILES", "ERRORS & LESSONS"):
            self.assertIn(section, content)


if __name__ == "__main__":
    unittest.main()