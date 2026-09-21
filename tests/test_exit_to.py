# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import shutil
import tempfile
import unittest

from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent

_O = chr(60) + "tool_code" + chr(62)
_C = chr(60) + "/tool_code" + chr(62)
TOOLS = "### Toolset\n\t- read_file\n"
GO = _O + '{"tool": "exit_to", "args": {"name": "%s"}}' + _C


class MockLLMProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []

    def generate(self, messages, **kwargs):
        resp = (self.responses[self.calls]
                if self.calls < len(self.responses) else "No response")
        self.calls += 1
        return resp, {"prompt_tokens": 10, "completion_tokens": 20}, None


def _md(max_loops=None, helper=False, c_target="C", a_exit="exit_to",
        a_allowed="exit_to"):
    p = ["## Xpa", "- **Code:** Xpa", "- **Model:** DefaultModel",
         "- **System Prompt:** > sys prom"]
    if max_loops is not None:
        p.append("- **Max Loops:** %d" % max_loops)
    a = ["- **A:**"]
    if a_allowed:
        a += ["    - **Allowed Tools:**"] + ["        - " + a_allowed]
    if a_exit:
        a += ["    - **Exit Tools:**"] + ["        - " + a_exit]
    a += ["    > STAGE_PROMPT_A"]
    p += a + ["- **B:**", "    > STAGE_PROMPT_B",
              "- **C:**",
              "    - **Allowed Tools:**", "        - exit_to",
              "    > STAGE_PROMPT_C"]
    if helper:
        p += ["- **Helper:**", "    - **Detached:** True",
              "    - **Allowed Tools:**", "        - exit_to",
              "    > STAGE_PROMPT_HELPER"]
    return "\n".join(p) + "\n" + TOOLS


class ExitToTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="yaha_exit_to_")
        d = os.path.join(self.ws, ".yaha")
        for sub in ("memory", "ext", "logs"):
            os.makedirs(os.path.join(d, sub))
        open(os.path.join(d, "tool-schema.md"), "w").write("[]")
        open(os.path.join(d, "Model.md"), "w").write("")
        self.d = d

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _agent(self, md, responses):
        open(os.path.join(self.d, "AgentClass.md"), "w").write(md)
        eng = YAHAEngine(workspace_dir=self.ws)
        cfg = eng.agent_classes.get("Xpa")
        ag = Agent(agent_config=cfg, session=eng.session,
                   models_config=eng.models_config,
                   agent_classes=eng.agent_classes,
                   tool_schemas=eng.tool_schemas,
                   access_controller=eng.access_controller)
        m = MockLLMProvider()
        m.responses = list(responses)
        ag.llm = m
        ag.unattended = True   # never block on the retry/break prompt
        return ag

    @staticmethod
    def _hist(ag):
        return "\n".join(str(x.get("content", "")) for x in ag.history)

    def _seen(self, ag):
        t = self._hist(ag)
        return [n for n in ("A", "B", "C", "HELPER") if "STAGE_PROMPT_" + n in t]

    def test_blank_name_is_an_error(self):
        ag = self._agent(_md(), [])
        self.assertTrue(ag.exit_to_impl().startswith("Error"))
        self.assertTrue(ag.exit_to_impl("  ").startswith("Error"))
        self.assertEqual(ag.exit_to_impl("A"), "EXIT_TO_SIGNALED")

    def test_resolver(self):
        ag = self._agent(_md(helper=True), [])
        self.assertEqual(ag._resolve_exit_to("b"), ("stage", "B"))
        self.assertEqual(ag._resolve_exit_to("helper"), ("call", "Helper"))
        self.assertIsNone(ag._resolve_exit_to("Nope"))

    def test_advertised_only_when_named(self):
        self.assertIn('"name": "exit_to"',
                      self._agent(_md(), []).build_system_prompt())
        md2 = ("## Xpa\n- **Code:** Xpa\n- **Model:** D\n"
               "- **System Prompt:** > s\n- **A:**\n    > STAGE_PROMPT_A\n" + TOOLS)
        self.assertNotIn('"name": "exit_to"',
                         self._agent(md2, []).build_system_prompt())

    def test_forward_jump_skips_intermediate(self):
        ag = self._agent(_md(c_target="C"), [GO % "C", "final answer"])
        out = ag.run("task")
        self.assertEqual(self._seen(ag), ["A", "C"])
        self.assertIn("final answer", str(out))
        self.assertEqual(ag.loop_count, 1)

    def test_backward_jump_reenters(self):
        ag = self._agent(_md(c_target="A"), [GO % "C", GO % "A", GO % "B", "done"])
        ag.run("task")
        self.assertEqual(self._seen(ag), ["A", "B", "C"])
        self.assertEqual(ag.loop_count, 3)

    def test_loop_back_is_a_uniform_jump(self):
        ag = self._agent(_md(c_target="A"), [GO % "C", GO % "B", "done"])
        ag.run("task")
        # The retired whole-chain loop state is gone: a jump is just a jump.
        self.assertFalse(hasattr(ag, "pending_loop_task"))
        self.assertEqual(ag.loop_count, 2)

    def test_unknown_target_is_a_hard_error(self):
        ag = self._agent(_md(), [GO % "Nope", GO % "C", "done"])
        ag.run("task")
        self.assertIn("exit_to target 'Nope'", self._hist(ag))
        self.assertEqual(self._seen(ag), ["A", "C"])

    def test_call_then_resume_at_caller_next(self):
        ag = self._agent(_md(helper=True),
                         [GO % "Helper", "helper done", "b done"])
        ag.run("task")
        t = self._hist(ag)
        order = [n for n in ("STAGE_PROMPT_A", "STAGE_PROMPT_HELPER",
                             "STAGE_PROMPT_B") if n in t]
        self.assertEqual(order, ["STAGE_PROMPT_A", "STAGE_PROMPT_HELPER",
                                 "STAGE_PROMPT_B"])

    def test_nested_call_cap_aborts(self):
        ag = self._agent(_md(helper=True), [GO % "Helper"] * 25)
        self.assertIn("call depth exceeded", str(ag.run("task")))

    def test_budget_counts_jumps(self):
        ag = self._agent(_md(max_loops=2, c_target="A"),
                         [GO % "C", GO % "A", GO % "B", "done"])
        self.assertEqual(ag.run("task"),
                         "Error: Maximum chain loops reached (2).")

    def test_exempt_from_exit_tools(self):
        ag = self._agent(_md(a_exit="success_tool"), [GO % "B", "done"])
        ag.run("task")
        # A exit_to(B) is EXEMPT from A Exit Tools (success_tool), so A jumps to B; B ends on plain text and C is simply the rest of the chain.
        self.assertEqual(self._seen(ag), ["A", "B", "C"])

    def test_rejected_when_not_allowed(self):
        # exit_to is not in A's Allowed Tools: the call is refused, the stage
        # stays alive, and the run continues until the model exits A with a
        # tool that IS allowed (no more max_turn to cut a stuck stage off).
        OK = _O + '{"tool": "success_tool", "args": {"summary": "s"}}' + _C
        ag = self._agent(_md(a_allowed="success_tool", a_exit="success_tool"),
                         [GO % "B", OK])
        ag.run("task")
        self.assertIn("not allowed in stage", self._hist(ag))


    def test_shipped_tool_schema_parses_and_exposes_exit_to(self):
        import pathlib as _p
        import yaha as _y
        from yaha.config.tools import load_tool_schema
        tmpl = _p.Path(_y.__file__).resolve().parent / "templates" / "tool-schema.md"
        entries = load_tool_schema(str(tmpl))
        self.assertTrue(entries)
        self.assertIn("exit_to", [e.get("name") for e in entries])

    def test_fail_target_resolving_to_the_stage_itself_aborts(self):
        # An EXPLICIT self fail target is a bug: abort loudly instead of
        # looping. (An UNRESOLVED target no longer reaches this guard -
        # it terminates via the built-in, undeclared Aftermath; see
        # tests/test_implicit_aftermath.py.)
        FAIL = _O + '{"tool": "fail_tool", "args": {"reason": "boom"}}' + _C
        md = ("## Xpa@@- **Code:** Xpa@@- **Model:** DefaultModel@@"
              "- **System Prompt:** > s@@"
              "- **A:**@@    > STAGE_PROMPT_A@@"
              "- **B:**@@"
              "    - **Allowed Tools:**@@        - fail_tool@@"
              "    - **Exit Tools:**@@        - fail_tool@@"
              "    - **Fail Target:** B@@"
              "    > STAGE_PROMPT_B@@" + TOOLS)
        md = md.replace("@@", chr(10))
        ag = self._agent(md, ["a is done", FAIL])
        self.assertIn("resolves to the stage itself", str(ag.run("task")))

    def test_self_jump_is_refused(self):
        # Naming the CURRENT stage is a loop, not a jump: refused, and the
        # stage stays alive (the next response still runs in A).
        ag = self._agent(_md(), [GO % "A", GO % "C", "done"])
        ag.run("task")
        self.assertIn("cannot target the CURRENT stage", self._hist(ag))
        self.assertEqual(self._seen(ag), ["A", "C"])

    def test_final_answer_curated_on_jump(self):
        # A Final Answer stage that ends via exit_to contributes its summary
        # to the curated final answer (uniform curation: DONE / LOOP /
        # EXIT_TO all count as completion).
        GO_S = _O + ('{"tool": "exit_to", "args": {"name": "C", '
                     '"summary": "a curated jump"}}') + _C
        md = ("## Xpa@@- **Code:** Xpa@@- **Model:** DefaultModel@@"
              "- **System Prompt:** > s@@"
              "- **A:**@@    - **Final Answer:** True@@"
              "    - **Allowed Tools:**@@        - exit_to@@"
              "    - **Exit Tools:**@@        - exit_to@@"
              "    > STAGE_PROMPT_A@@"
              "- **B:**@@    > STAGE_PROMPT_B@@"
              "- **C:**@@    > STAGE_PROMPT_C@@" + TOOLS)
        md = md.replace("@@", chr(10))
        ag = self._agent(md, [GO_S, "c answer"])
        out = str(ag.run("task"))
        self.assertIn("--- A and task ---", out)
        self.assertIn("a curated jump", out)

    def test_failed_stage_is_not_curated(self):
        # A stage that FAILS never contributes to the final answer, even with
        # Final Answer set: the chain's answer is the next completed stage's.
        FAIL = _O + '{"tool": "fail_tool", "args": {"reason": "a reason"}}' + _C
        md = ("## Xpa@@- **Code:** Xpa@@- **Model:** DefaultModel@@"
              "- **System Prompt:** > s@@"
              "- **A:**@@    - **Final Answer:** True@@"
              "    - **Allowed Tools:**@@        - fail_tool@@"
              "    - **Exit Tools:**@@        - fail_tool@@"
              "    - **Fail Target:** B@@"
              "    > STAGE_PROMPT_A@@"
              "- **B:**@@    > STAGE_PROMPT_B@@" + TOOLS)
        md = md.replace("@@", chr(10))
        ag = self._agent(md, [FAIL, "b answer"])
        out = str(ag.run("task"))
        self.assertEqual(out, "b answer")

    def test_required_preexit_in_front_of_exit_to_abandons_the_move(self):
        md = ("## Xpa@@- **Code:** Xpa@@- **Model:** DefaultModel@@"
              "- **System Prompt:** > s@@"
              "- **A:**@@"
              "    - **Allowed Tools:**@@        - exit_to@@"
              "    - **preexit:**@@"
              "        - bash (required): |@@            exit 13@@"
              "    > STAGE_PROMPT_A@@"
              "- **B:**@@    > STAGE_PROMPT_B@@" + TOOLS)
        md = md.replace("@@", chr(10))
        ag = self._agent(md, [GO % "B", "done"])
        ag.run("task")
        hist = self._hist(ag)
        # the gate is NOT bypassable by exit_to: the required preexit hook
        # runs in front of the move and its failure is recorded; the stage
        # then fails through its FAIL TARGET (it does not jump to B).
        self.assertIn("required hook A/preexit", hist)
        self.assertIn("failed", hist)


if __name__ == "__main__":
    unittest.main()
