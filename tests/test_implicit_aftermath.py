# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Aftermath is a built-in stage: it always exists, declared or not.

Undeclared, it has no prompt, so a failure routed to it terminates the
chain IMMEDIATELY - collecting every final-answer output gathered so
far - instead of falling back to the last stage (which may be the
current one: the zero-progress hang this regression suite pins down).
"""
import os
import shutil
import tempfile
import unittest

from yaha.core.engine import YAHAEngine
from yaha.core.agent import Agent

TOOLS = "### Toolset\n\t- read_file\n"


class MockLLMProvider:
    def __init__(self):
        self.calls = 0
        self.responses = []

    def generate(self, messages, **kwargs):
        resp = (self.responses[self.calls]
                if self.calls < len(self.responses) else "No response")
        self.calls += 1
        return resp, {"prompt_tokens": 10, "completion_tokens": 20}, None


def _stage(name, *lines):
    return (["- **%s:**" % name] + list(lines)
            + ["    > STAGE_PROMPT_%s" % name])


def _md(*stages):
    p = ["## Xpa", "- **Code:** Xpa", "- **Model:** DefaultModel",
         "- **System Prompt:** > sys"]
    for s in stages:
        p += s
    return "\n".join(p) + "\n" + TOOLS


# A stage that always fails: the mock calls fail_tool, routing the stage
# through its fail target (default: aftermath).
_O = chr(60) + "tool_code" + chr(62)
_C = chr(60) + "/tool_code" + chr(62)
FAIL_CALL = _O + '{"tool": "fail_tool", "args": {"reason": "stage broken"}}' + _C

FAILING_A = _stage("A",
                   "    - **Allowed Tools:**",
                   "        - fail_tool",
                   "    - **Exit Tools:**",
                   "        - fail_tool")

FAILING_B = _stage("B",
                   "    - **Allowed Tools:**",
                   "        - fail_tool",
                   "    - **Exit Tools:**",
                   "        - fail_tool")

PLAIN_B = _stage("B")


class ImplicitAftermathTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="yaha_implicit_aftermath_")
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

    def test_undeclared_aftermath_terminates_instead_of_falling_back(self):
        # Chain [A, B] with no Aftermath: A fails. The old fallback sent
        # the walk to the LAST stage (B); the built-in Aftermath has no
        # prompt, so the chain must terminate with B never entered.
        ag = self._agent(_md(FAILING_A, PLAIN_B), [FAIL_CALL])
        result = str(ag.run("task"))
        self.assertIn("--- Failure (A) ---", result)
        self.assertIn("stage broken", result)
        self.assertNotIn("STAGE_PROMPT_B", self._hist(ag))

    def test_termination_collects_all_final_answers(self):
        # A is a final-answer stage and completes; B fails; C must never
        # run. The termination answer keeps A's output AND names the
        # failure, so a partial result cannot be mistaken for a success.
        a_final = _stage("A", "    - **Final Answer:** True")
        c = _stage("C")
        ag = self._agent(_md(a_final, FAILING_B, c),
                         ["alpha answer", FAIL_CALL])
        result = str(ag.run("task"))
        self.assertIn("--- A and task ---", result)
        self.assertIn("alpha answer", result)
        self.assertIn("--- Failure (B) ---", result)
        self.assertNotIn("STAGE_PROMPT_C", self._hist(ag))

    def test_unknown_explicit_fail_target_also_terminates(self):
        # A declares a fail target that does not exist: by convention it
        # routes to the built-in Aftermath - undeclared, so terminate.
        a = _stage("A",
                   "    - **Allowed Tools:**",
                   "        - fail_tool",
                   "    - **Exit Tools:**",
                   "        - fail_tool",
                   "    - **Fail Target:** NoSuch")
        ag = self._agent(_md(a, PLAIN_B), [FAIL_CALL])
        result = str(ag.run("task"))
        self.assertIn("--- Failure (A) ---", result)
        self.assertNotIn("STAGE_PROMPT_B", self._hist(ag))

    def test_declared_aftermath_still_runs(self):
        # A declared Aftermath is a real stage: the failure routes there
        # and the chain completes with its output.
        aftermath = _stage("Aftermath")
        ag = self._agent(_md(FAILING_A, aftermath),
                         [FAIL_CALL, "cleanup done"])
        result = str(ag.run("task"))
        self.assertIn("cleanup done", result)
        self.assertIn("STAGE_PROMPT_Aftermath", self._hist(ag))
        self.assertNotIn("--- Failure", result)

    def test_failing_required_preenter_terminates_the_same_way(self):
        # The preenter routing shares the rule: undeclared Aftermath
        # terminates the chain immediately instead of entering B.
        a_hook = ["- **A:**",
                  "    - **preenter:**",
                  "        - bash (required): exit 1",
                  "    > STAGE_PROMPT_A"]
        ag = self._agent(_md(a_hook, PLAIN_B), ["plain"])
        result = str(ag.run("task"))
        self.assertIn("--- Failure (A) ---", result)
        self.assertIn("required hook", result)
        self.assertNotIn("STAGE_PROMPT_B", self._hist(ag))

    def test_failure_inside_aftermath_ends_the_chain_normally(self):
        # Aftermath is the terminal stage: it has NO fail target. A failure
        # IN Aftermath (fail_tool here) ends the chain normally - the final
        # answer is the curated outputs plus a Failure (Aftermath) block,
        # never an ERROR status.
        aftermath = _stage("Aftermath",
                           "    - **Allowed Tools:**",
                           "        - fail_tool",
                           "    - **Exit Tools:**",
                           "        - fail_tool")
        fail_aftermath = (_O + '{"tool": "fail_tool", "args": '
                          '{"reason": "cannot recover"}}' + _C)
        ag = self._agent(_md(FAILING_A, aftermath),
                         [FAIL_CALL, fail_aftermath])
        result = str(ag.run("task"))
        self.assertIn("--- Failure (Aftermath) ---", result)
        self.assertIn("cannot recover", result)
        self.assertNotIn("Error: Stage 'Aftermath' failed.", result)


if __name__ == "__main__":
    unittest.main()
