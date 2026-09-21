#!/usr/bin/env python3
'''Wait Event tests: AgentClass parsing, inheritance and stage-boundary gating.

A stage that declares Wait Event is not entered until the named event is set;
the scheduler sees a ('wait', name) token instead of ('enter', name). Built
over a real Agent with a scripted LLM, so no network or TTY is used.
'''
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.config.agents import parse_agent_class_md
from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import AgentRuntime, READY, WAITING

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

FIXTURE = '''
## Waiter
- **Inherited:** None
- **Code:** Wtr
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Aftermath:**
    > done

## WaiterChild
- **Inherited:** Waiter
- **Code:** Wch
- **Work:**
    - **Inherited:** All

## WaiterOverride
- **Inherited:** Waiter
- **Code:** Wov
- **Work:**
    - **Inherited:** All
    - **Wait Event:** other

## Plain
- **Inherited:** None
- **Code:** Pln
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    > work
- **Aftermath:**
    > done
## Signaller
- **Inherited:** None
- **Code:** Sgl
- **Model:** m
- **Preamble:**
    > go
- **Work:**
    - **Exit Tools:**
        - set_event_success
    > work
- **Aftermath:**
    > done

## Looper
- **Inherited:** None
- **Code:** Lpr
- **Model:** m
- **Preamble:**
    > go
- **Work:**
    - **Exit Tools:**
        - set_event_loop
    > work
- **Aftermath:**
    > done

## Rearmer
- **Inherited:** None
- **Code:** Rrm
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Work2:**
    - **Wait Event:** go
    > work2
- **Aftermath:**
    > done

## LoopWaiter
- **Inherited:** None
- **Code:** Lwt
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Aftermath:**
    > done
### Toolset
	- loop_tool

'''


def _def_stage(name, chain):
    return next((s for s in chain if s['name'] == name), None)


def _write_fixture():
    TMP.mkdir(parents=True, exist_ok=True)
    path = TMP / 'test_stage_wait_event_fixture.md'
    path.write_text(FIXTURE, encoding='utf-8')
    return path


class _MockLLM:
    def __init__(self, response='ok'):
        self.response = response
        self.calls = 0
        self.usage = {'prompt_tokens': 5, 'completion_tokens': 5}

    def generate(self, messages, **kwargs):
        self.calls += 1
        return self.response, dict(self.usage), None


def _make_engine():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_ma_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    return YAHAEngine(workspace_dir=str(ws)), ws


def _make_agent(eng, class_name='Waiter'):
    cfg = eng.agent_classes[class_name]
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    agent.llm = _MockLLM('ok')
    return agent


class ParsingTests(unittest.TestCase):
    def test_wait_event_is_parsed(self):
        cls = parse_agent_class_md(str(_write_fixture()))
        work = _def_stage('Work', cls['Waiter'].prompt_chain)
        self.assertEqual(work.get('wait_event'), 'go')
        self.assertIn('wait_event', work.get('declared_fields'))

    def test_absent_wait_event_is_none(self):
        cls = parse_agent_class_md(str(_write_fixture()))
        pre = _def_stage('Preamble', cls['Waiter'].prompt_chain)
        self.assertIsNone(pre.get('wait_event'))

    def test_wait_event_inherits_with_inherited_all(self):
        cls = parse_agent_class_md(str(_write_fixture()))
        work = _def_stage('Work', cls['WaiterChild'].prompt_chain)
        self.assertEqual(work.get('wait_event'), 'go')

    def test_child_wait_event_overrides_inherited(self):
        cls = parse_agent_class_md(str(_write_fixture()))
        work = _def_stage('Work', cls['WaiterOverride'].prompt_chain)
        self.assertEqual(work.get('wait_event'), 'other')


class GatingTests(unittest.TestCase):
    def test_blocked_stage_waits_then_enters_when_set(self):
        eng, ws = _make_engine()
        try:
            agent = _make_agent(eng, 'Waiter')
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            gen = agent.iter_run('task')
            self.assertEqual(next(gen), ('enter', 'Preamble'))
            self.assertEqual(next(gen), ('wait', 'go'))
            rt.set_event('go')
            self.assertEqual(next(gen), ('enter', 'Work'))
            self.assertEqual(next(gen), ('enter', 'Aftermath'))
            with self.assertRaises(StopIteration) as ctx:
                next(gen)
            self.assertIsInstance(ctx.exception.value, str)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_stage_without_wait_event_enters_directly(self):
        eng, ws = _make_engine()
        try:
            agent = _make_agent(eng, 'Plain')
            agent.runtime = AgentRuntime(ui=agent.ui)
            gen = agent.iter_run('task')
            self.assertEqual(next(gen), ('enter', 'Preamble'))
            self.assertEqual(next(gen), ('enter', 'Work'))
            self.assertEqual(next(gen), ('enter', 'Aftermath'))
            with self.assertRaises(StopIteration):
                next(gen)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_single_agent_run_on_unset_event_raises_deadlock(self):
        eng, ws = _make_engine()
        try:
            agent = _make_agent(eng, 'Waiter')
            agent.runtime = AgentRuntime(ui=agent.ui)
            from yaha.core.runtime import RuntimeDeadlockError
            with self.assertRaises(RuntimeDeadlockError):
                agent.run('task')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_two_stages_with_same_wait_event_each_block(self):
        """Re-arm through a REAL agent driven by the runtime.

        Work and Work2 both declare ``Wait Event: go``. The first pulse
        releases the Work wait; the SAME event must park the agent AGAIN at
        Work2 (auto-reset) instead of passing straight through.
        """
        eng, ws = _make_engine()
        try:
            agent = _make_agent(eng, 'Rearmer')
            rt = AgentRuntime(ui=agent.ui)
            agent.runtime = rt
            handle = rt.spawn(agent, 'task', name='r', tag='r')

            rt._step(handle)              # -> enter Preamble
            self.assertEqual(handle.entered, ['Preamble'])

            rt._step(handle)              # Preamble runs; Work gate blocks
            self.assertEqual(handle.status, WAITING)
            self.assertEqual(handle.wait_event, 'go')

            rt.set_event('go')            # one pulse releases AND re-arms
            self.assertEqual(handle.status, READY)
            self.assertFalse(rt.event_is_set('go'))

            rt._step(handle)              # -> enter Work
            self.assertEqual(handle.entered, ['Preamble', 'Work'])

            rt._step(handle)              # Work2 gate must block AGAIN
            self.assertEqual(handle.entered, ['Preamble', 'Work'])
            self.assertEqual(handle.status, WAITING)
            self.assertEqual(handle.wait_event, 'go')

            rt.set_event('go')            # second pulse
            rt._step(handle)              # -> enter Work2
            self.assertEqual(handle.entered, ['Preamble', 'Work', 'Work2'])
            rt._step(handle)              # -> enter Aftermath
            self.assertEqual(
                handle.entered, ['Preamble', 'Work', 'Work2', 'Aftermath'])
        finally:
            shutil.rmtree(ws, ignore_errors=True)



def _t_exit_tools_terminator_is_advertised(self):
    eng, ws = _make_engine()
    try:
        sig = _make_agent(eng, 'Signaller')
        self.assertIn('set_event_success', sig.build_system_prompt())
        plain = _make_agent(eng, 'Waiter')
        self.assertNotIn('set_event_success', plain.build_system_prompt())
    finally:
        shutil.rmtree(ws, ignore_errors=True)


GatingTests.test_exit_tools_terminator_is_advertised = _t_exit_tools_terminator_is_advertised



def _t_set_event_loop_documents_current_stage_loop(self):
    eng, ws = _make_engine()
    try:
        looper = _make_agent(eng, 'Looper')
        prompt = looper.build_system_prompt()
        self.assertIn('set_event_loop', prompt)
        self.assertIn('CURRENT stage', prompt)
        self.assertNotIn('WHOLE chain', prompt)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _t_shipped_tool_schema_wording_is_corrected(self):
    from pathlib import Path
    import yaha
    tmpl = Path(yaha.__file__).resolve().parent / 'templates' / 'tool-schema.md'
    text = tmpl.read_text(encoding='utf-8')
    self.assertIn('set_event_loop', text)
    self.assertIn('CURRENT stage', text)
    self.assertNotIn('WHOLE chain', text)
    self.assertNotIn('usable as an event target', text)
    self.assertIn('named events', text)
    self.assertIn('unbounded', text)


GatingTests.test_set_event_loop_documents_current_stage_loop = _t_set_event_loop_documents_current_stage_loop
GatingTests.test_shipped_tool_schema_wording_is_corrected = _t_shipped_tool_schema_wording_is_corrected


def _t_agentclass_events_doc_describes_auto_reset(self):
    from _template_fixture import agentclass_doc_text
    # The authoring/events documentation moved out of the template header into
    # the shipped `docs/agentclass.md` when the template became include-based.
    text = agentclass_doc_text()
    # The shipped docs must describe the auto-reset (one-shot pulse) policy
    # and must NOT still claim the old sticky, manual-reset behaviour.
    self.assertIn('AUTO-RESET', text)
    self.assertIn('blocks on EVERY round', text)
    self.assertNotIn('NEVER cleared automatically', text)
    self.assertNotIn('manual-reset', text)


GatingTests.test_agentclass_events_doc_describes_auto_reset = (
    _t_agentclass_events_doc_describes_auto_reset
)


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, messages, **kwargs):
        resp = (self.responses[self.calls]
                if self.calls < len(self.responses) else 'ok')
        self.calls += 1
        return resp, {'prompt_tokens': 5, 'completion_tokens': 5}, None


def _t_loop_reentry_rechecks_the_wait_gate(self):
    """A stage that loops ITSELF is parked on its Wait Event again.

    Work declares ``Wait Event: go`` and its first entry ends with
    ``loop_tool`` (loop the CURRENT stage, no advance). The loop-top gate is
    re-checked on re-entry: the one pulse that released the first entry is
    already consumed (auto-reset), so the agent must park AGAIN until a
    second pulse arrives.
    """
    eng, ws = _make_engine()
    try:
        agent = _make_agent(eng, 'LoopWaiter')
        loop_call = (chr(60) + 'tool_code' + chr(62)
                     + '{"tool": "loop_tool", "args": {"modifier": "again"}}'
                     + chr(60) + '/tool_code' + chr(62))
        agent.llm = _ScriptedLLM(['ok', loop_call, 'ok', 'ok'])
        rt = AgentRuntime(ui=agent.ui)
        agent.runtime = rt
        handle = rt.spawn(agent, 'task', name='lw', tag='lw')

        rt._step(handle)              # -> enter Preamble
        rt._step(handle)              # Work gate blocks on the first pass
        self.assertEqual(handle.status, WAITING)
        self.assertEqual(handle.wait_event, 'go')

        rt.set_event('go')            # first pulse releases AND re-arms
        rt._step(handle)              # -> enter Work; loop_tool loops the stage
        self.assertEqual(handle.entered, ['Preamble', 'Work'])

        rt._step(handle)              # the LOOPED re-entry must park AGAIN
        self.assertEqual(handle.status, WAITING)
        self.assertEqual(handle.wait_event, 'go')
        self.assertEqual(handle.entered, ['Preamble', 'Work'])

        rt.set_event('go')            # second pulse
        rt._step(handle)              # -> enter Work again, then finish
        self.assertEqual(handle.entered, ['Preamble', 'Work', 'Work'])
        rt._step(handle)
        self.assertEqual(
            handle.entered, ['Preamble', 'Work', 'Work', 'Aftermath'])
        # The modifier rode exactly ONE re-entry of the looped stage.
        blob = ' '.join(str(m.get('content') or '') for m in agent.history)
        self.assertEqual(blob.count('[TASK MODIFIER]: again'), 1)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


GatingTests.test_loop_reentry_rechecks_the_wait_gate = (
    _t_loop_reentry_rechecks_the_wait_gate
)


if __name__ == '__main__':
    unittest.main()
