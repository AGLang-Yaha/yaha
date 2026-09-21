#!/usr/bin/env python3
'''spawn_parallel_agent tests: new vs clone context, authorization and handles.

The engine registers a peer on the cooperative runtime (non-blocking). 'new'
gives the child a fresh context window; 'clone' seeds it from a deep copy of
the caller's history. No LLM call happens at spawn time.
'''
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import AgentRuntime

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'

FIXTURE = '''
## Parent
- **Inherited:** None
- **Code:** Par
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > parent
- **Aftermath:**
    > parent done

### Agentset
- Child

## Child
- **Inherited:** None
- **Code:** Chd
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > child
- **Aftermath:**
    > child done

### Agentset
- Grandchild

## Grandchild
- **Inherited:** None
- **Code:** Gch
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > grandchild
- **Aftermath:**
    > grandchild done

## Blocker
- **Inherited:** None
- **Code:** Blk
- **Model:** m
- **Preamble:**
    > go
- **Work:**
    - **Wait Event:** never_set
    > work
- **Aftermath:**
    > done

## Stranger
- **Inherited:** None
- **Code:** Str
- **Model:** m
- **Toolset:**
    - bash_tool
- **Preamble:**
    > stranger
- **Aftermath:**
    > stranger done
'''

PARENT_MARKER = 'PARENT_CONTEXT_MARKER'


def _make_engine():
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_spawn_', dir=str(TMP)))
    yh = ws / '.yaha'
    yh.mkdir(parents=True)
    (yh / 'memory').mkdir()
    (yh / 'ext').mkdir()
    (yh / 'logs').mkdir()
    (yh / 'tool-schema.md').write_text('[]')
    (yh / 'AgentClass.md').write_text(FIXTURE)
    (yh / 'Model.md').write_text('')
    return YAHAEngine(workspace_dir=str(ws)), ws


def _make_parent(eng):
    cfg = eng.agent_classes['Parent']
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    agent.history = [
        {'role': 'system', 'content': 'parent-system'},
        {'role': 'user', 'content': PARENT_MARKER},
    ]
    return agent


def _only_handle(rt):
    return list(rt.pool.values())[0]


class SpawnTests(unittest.TestCase):
    def test_new_mode_gives_a_fresh_context(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            msg = eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'new', 'do it')
            self.assertIn('Spawned parallel agent', msg)
            handle = _only_handle(rt)
            next(handle.generator)
            contents = [m.get('content') for m in handle.agent.history]
            self.assertNotIn(PARENT_MARKER, contents)
            self.assertEqual(handle.agent.history[0]['role'], 'system')
            self.assertEqual(handle.agent.runtime, rt)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_clone_mode_copies_parent_context(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'clone', 'do it')
            handle = _only_handle(rt)
            next(handle.generator)
            contents = [m.get('content') for m in handle.agent.history]
            self.assertIn(PARENT_MARKER, contents)
            self.assertEqual(handle.agent.history[0]['role'], 'system')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_clone_is_isolated_from_parent_after_spawn(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            parent_len = len(parent.history)
            rt = AgentRuntime(ui=parent.ui)
            eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'clone', 'do it')
            handle = _only_handle(rt)
            next(handle.generator)
            handle.agent.history.append({'role': 'user', 'content': 'child-only'})
            self.assertEqual(len(parent.history), parent_len)
            contents = [m.get('content') for m in parent.history]
            self.assertNotIn('child-only', contents)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unauthorized_class_is_rejected(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            msg = eng._spawn_parallel_agent(rt, parent, parent.config, 'Stranger', 'new', 'x')
            self.assertTrue(msg.startswith('Error'), msg)
            self.assertEqual(rt.pool, {})
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_unknown_class_is_rejected(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            msg = eng._spawn_parallel_agent(rt, parent, parent.config, 'NoSuchClass', 'new', 'x')
            self.assertTrue(msg.startswith('Error'), msg)
            self.assertEqual(rt.pool, {})
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_invalid_clone_context_is_rejected(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            msg = eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'copy', 'x')
            self.assertIn("must be 'new' or 'clone'", msg)
            self.assertEqual(rt.pool, {})
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_two_spawns_get_distinct_handles_on_one_registry(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'new', 'a')
            eng._spawn_parallel_agent(rt, parent, parent.config, 'Child', 'clone', 'b')
            names = sorted(rt.pool)
            self.assertEqual(len(names), 2)
            self.assertNotEqual(names[0], names[1])
            for h in rt.pool.values():
                self.assertIs(h.agent.runtime.events, rt.events)
            self.assertEqual(rt.ready.__len__(), 2)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_inherited_spawns_the_parent_class(self):
        eng, ws = _make_engine()
        try:
            parent = _make_parent(eng)
            rt = AgentRuntime(ui=parent.ui)
            msg = eng._spawn_parallel_agent(rt, parent, parent.config, 'Inherited', 'new', 'x')
            self.assertIn('Spawned parallel agent', msg)
            handle = _only_handle(rt)
            self.assertEqual(handle.agent.config.name, 'Parent')
        finally:
            shutil.rmtree(ws, ignore_errors=True)



def _t_peer_can_itself_spawn(self):
    eng, ws = _make_engine()
    try:
        parent = _make_parent(eng)
        rt = AgentRuntime(ui=parent.ui)
        eng._wire_parallel_spawner(parent, parent.config, rt)
        msg = parent.spawn_parallel_agent('Child', 'new', 'x')
        self.assertIn('Spawned parallel agent', msg)
        peers = [h.agent for h in rt.pool.values() if h.agent.config.name == 'Child']
        self.assertEqual(len(peers), 1)
        peer = peers[0]
        msg2 = peer.spawn_parallel_agent('Grandchild', 'new', 'y')
        self.assertIn('Spawned parallel agent', msg2)
        self.assertEqual(len(rt.pool), 2)
        msg3 = peer.spawn_parallel_agent('Stranger', 'new', 'z')
        self.assertTrue(msg3.startswith('Error'), msg3)
        self.assertEqual(len(rt.pool), 2)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


SpawnTests.test_peer_can_itself_spawn = _t_peer_can_itself_spawn



class _FakeProvider:
    def __init__(self, cfg=None):
        pass

    def generate(self, messages, **kwargs):
        return 'ok', {'prompt_tokens': 1, 'completion_tokens': 1}, None


def _t_nested_wait_event_gives_a_clear_error(self):
    eng, ws = _make_engine()
    try:
        with patch('yaha.llm.ollama.OllamaProvider', _FakeProvider):
            result = eng.run_agent('Blocker', 'task', caller_id='parent-id')
        self.assertTrue(result.startswith('Error'), result)
        self.assertIn('Wait Event', result)
        self.assertIn('spawn_parallel_agent', result)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


SpawnTests.test_nested_wait_event_gives_a_clear_error = _t_nested_wait_event_gives_a_clear_error


if __name__ == '__main__':
    unittest.main()
