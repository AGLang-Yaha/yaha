#!/usr/bin/env python3
'''`-I/--include` extra class files must resolve inheritance like an include.

An extra class file given on the command line is resolved EXACTLY as if its text
had been appended to the workspace's `.yaha/AgentClass.md` (an `<!--include-->` at
the end), so a class in an extra file inherits from every class the workspace
defines - and from classes in earlier `-I` files, in the order given.

Before this fix the engine parsed each extra file on its own and only THEN merged
the result into the class map, so a parent that lived in the workspace resolved
to nothing: the class kept its own tools, had an EMPTY system prompt, lost the
parent's detached stages, and its model stayed empty - which made a run fall back
to the first model entry of `.yaha/Model.md` (typically `ollama_default`).
'''
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.config.agents import parse_agent_class_md, parse_agent_class_text
from yaha.core.engine import YAHAEngine
from yaha.core.md_loader import MarkdownLoadError

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'
TEMPLATES = HOME / 'yaha' / 'templates'
EXAMPLE = HOME / 'examples' / 'echo_agent' / 'echo_agent.md'

BASE_FIXTURE = '''
## Alpha
- **Inherited:** None
- **Code:** Alp
- **Description:** The primary workspace class.
- **Model:** DefaultModel
- **System Prompt:**
    > ALPHA PROTOCOL
- **Toolset:**
    - bash_tool
- **Preamble:**
    > do the thing
- **Aftermath:**
    > done
'''


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def workspace_dir():
    '''A temporary workspace directory with the standard .yaha layout.'''
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_iflag_', dir=str(TMP)))
    yaha_dir = ws / '.yaha'
    for sub in ('memory', 'ext', 'logs'):
        (yaha_dir / sub).mkdir(parents=True)
    return ws


def template_workspace():
    '''A workspace built the way ``yaha --init`` builds one.'''
    ws = workspace_dir()
    for template in sorted(TEMPLATES.glob('*.md')):
        shutil.copy(template, ws / '.yaha' / template.name)
    return ws


def inspect(config):
    '''The facets a resolved class carries, for equality comparisons.'''
    return {
        'inherited': config.inherited,
        'model': config.model,
        'system_prompt': config.system_prompt,
        'toolset': sorted(config.toolset),
        'agentset': sorted(config.agentset),
        'snap_handler': config.snap_handler,
        'snap_threshold': config.snap_threshold,
        'snap_to': config.snap_to,
        'detached': sorted((config.detached_stages or {}).keys()),
        'chain': [(s.get('name'), s.get('wait_event')) for s in config.prompt_chain],
    }


class ExtraFileInheritanceTests(unittest.TestCase):
    '''T1/T2: an extra file resolves inheritance, including across extra files.'''

    def tearDown(self):
        for attr in ('workspace', 'extra_dir'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_an_extra_class_inherits_the_workspaces_class(self):
        '''T1: the exact failure - the parent lives in AgentClass.md, not in -I.'''
        self.workspace = template_workspace()
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_extra_', dir=str(TMP)))
        extra = _write(self.extra_dir / 'child.md', '''
## Child
- **Inherited:** BaseAgent
- **Code:** Chd
- **Description:** Defined in an extra file.
- **Toolset:**
    - **Inherited:** All
    - connect_tool
- **Preamble:**
    > extra stage
- **Aftermath:**
    > done
''')
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(extra)])
        parent = engine.agent_classes['BaseAgent']
        child = engine.agent_classes['Child']
        self.assertEqual(child.inherited, 'BaseAgent')
        self.assertEqual(child.model, parent.model)
        self.assertTrue(child.system_prompt.strip(),
                        'the inherited system prompt is empty')
        self.assertEqual(child.system_prompt, parent.system_prompt)
        self.assertTrue(set(parent.toolset) <= set(child.toolset))
        self.assertIn('connect_tool', child.toolset)
        self.assertIn('Compression', child.detached_stages)
        self.assertIn('Preamble', [s['name'] for s in child.prompt_chain])

    def test_an_extra_class_can_inherit_from_an_earlier_extra_file(self):
        '''T2: extras are appended in order, so the second can see the first.'''
        self.workspace = template_workspace()
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_extra_', dir=str(TMP)))
        middle = _write(self.extra_dir / 'a_middle.md', '''
## Middle
- **Inherited:** BaseAgent
- **Code:** Mid
- **Description:** Middle class.
- **Preamble:**
    > middle
- **Aftermath:**
    > done
''')
        leaf = _write(self.extra_dir / 'b_leaf.md', '''
## Leaf
- **Inherited:** Middle
- **Code:** Lef
- **Description:** Leaf class.
- **Preamble:**
    > leaf
- **Aftermath:**
    > done
''')
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(middle), str(leaf)])
        parent = engine.agent_classes['BaseAgent']
        resolved = engine.agent_classes['Leaf']
        self.assertEqual(resolved.inherited, 'Middle')
        self.assertTrue(resolved.system_prompt.strip())
        self.assertEqual(resolved.system_prompt, parent.system_prompt)
        self.assertTrue(set(parent.toolset) <= set(resolved.toolset))
        # order matters: the leaf alone cannot resolve its parent
        alone = parse_agent_class_md(str(leaf))
        self.assertEqual(alone['Leaf'].inherited, 'Middle')
        self.assertEqual(alone['Leaf'].system_prompt, '')


class IncludeEquivalenceTests(unittest.TestCase):
    '''T4: `-I` resolves the same CLASS as an include does.

    Not the same MECHANISM: an include is macro processing that inlines text into
    one document, while `-I` parses each file as its own document and merges the
    class maps (see ``tests/test_include_flag_refactor.py``). What is asserted
    here is that the resulting CLASS is the same on every field it is made of:
    inherited, model, system prompt, toolset, agentset, snap fields, detached
    stages and the chain. Macro (`<!--ref-->`) expansion is the documented
    exception and stays per-file; see ``MacroBoundaryTests``.
    '''

    def tearDown(self):
        for attr in ('include_ws', 'flag_ws', 'shared'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_include_and_include_flag_resolve_the_same_class(self):
        self.shared = Path(tempfile.mkdtemp(prefix='yaha_shared_', dir=str(TMP)))
        extra_text = '''
## Squared
- **Inherited:** BaseAgent
- **Code:** Sqd
- **Description:** Same class, two install methods.
- **Toolset:**
    - **Inherited:** All
    - send_tool
- **Preamble:**
    - **Wait Event:** @gate
    - **Exit Tools:**
        - success_tool
    > squared
- **Aftermath:**
    > done
'''
        extra = _write(self.shared / 'squared.md', extra_text)

        # (a) installed with an include line, the file living in .yaha/
        self.include_ws = template_workspace()
        shutil.copy(str(extra), str(self.include_ws / '.yaha' / 'squared.md'))
        with (self.include_ws / '.yaha' / 'AgentClass.md').open('a') as handle:
            handle.write('\n\n<!--include path="squared.md" -->\n')
        by_include = YAHAEngine(workspace_dir=str(self.include_ws))

        # (b) installed with -I, the file living outside the workspace
        self.flag_ws = template_workspace()
        by_flag = YAHAEngine(workspace_dir=str(self.flag_ws),
                             include_agent_files=[str(extra)])

        left = inspect(by_include.agent_classes['Squared'])
        right = inspect(by_flag.agent_classes['Squared'])
        self.assertEqual(left, right)
        self.assertEqual(left['model'], 'DefaultModel')
        self.assertTrue(left['system_prompt'].strip())


class OverrideTests(unittest.TestCase):
    '''T3: a same-named class defined later still wins (unchanged behaviour).'''

    def tearDown(self):
        for attr in ('workspace', 'extra_dir'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_a_same_named_extra_class_overrides_the_workspace_one(self):
        self.workspace = workspace_dir()
        _write(self.workspace / '.yaha' / 'AgentClass.md', BASE_FIXTURE)
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_extra_', dir=str(TMP)))
        extra = _write(self.extra_dir / 'override.md', '''
## Alpha
- **Inherited:** None
- **Code:** OvA
- **Description:** The overriding definition.
- **Preamble:**
    > overridden
- **Aftermath:**
    > done
''')
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(extra)])
        alpha = engine.agent_classes['Alpha']
        self.assertEqual(alpha.code, 'OvA')
        self.assertEqual(alpha.description, 'The overriding definition.')


class EchoExampleThroughFlagTests(unittest.TestCase):
    '''T5: the reported reproduction, now working through `-I`.'''

    def tearDown(self):
        if getattr(self, 'workspace', None) is not None:
            shutil.rmtree(str(self.workspace), ignore_errors=True)

    def test_the_shipped_example_resolves_inheritance_via_include_flag(self):
        self.workspace = template_workspace()
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(EXAMPLE)])
        parent = engine.agent_classes['BaseAgent']
        for name in ('EchoServer', 'EchoClient'):
            resolved = engine.agent_classes[name]
            self.assertEqual(resolved.inherited, 'BaseAgent')
            self.assertEqual(resolved.model, parent.model)
            self.assertTrue(resolved.system_prompt.strip())
            self.assertIn('bash_tool', resolved.toolset)
            self.assertIn('Compression', resolved.detached_stages)


class LoadPathBackCompatTests(unittest.TestCase):
    '''T6: the refactor changed nothing for the paths that already worked.'''

    def tearDown(self):
        for attr in ('workspace', 'extra_dir'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_a_missing_extra_file_is_skipped_and_a_later_one_still_loads(self):
        self.workspace = workspace_dir()
        _write(self.workspace / '.yaha' / 'AgentClass.md', BASE_FIXTURE)
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_extra_', dir=str(TMP)))
        good = _write(self.extra_dir / 'good.md', '\n## Beta\n- **Inherited:** None\n'
                                                    '- **Code:** Bet\n- **Description:** b\n'
                                                    '- **Preamble:**\n    > b\n'
                                                    '- **Aftermath:**\n    > done\n')
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(self.extra_dir / 'nope.md'),
                                                 str(good)])
        self.assertIn('Beta', engine.agent_classes)
        self.assertIn('Alpha', engine.agent_classes)

    def test_a_missing_primary_file_still_yields_an_empty_map(self):
        self.workspace = workspace_dir()
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        self.assertEqual(engine.agent_classes, {})

    def test_single_file_parsing_is_unchanged(self):
        '''No extras means exactly the old behaviour: no cross-file parent.'''
        classes = parse_agent_class_md(str(EXAMPLE))
        self.assertEqual(sorted(classes), ['EchoClient', 'EchoServer'])
        server = classes['EchoServer']
        self.assertEqual(server.inherited, 'BaseAgent')
        self.assertEqual(server.system_prompt, '')     # parent not in this text
        self.assertNotIn('bash_tool', server.toolset)
        self.assertEqual([s['name'] for s in server.prompt_chain],
                         ['Preamble', 'Echo', 'Aftermath'])

    def test_the_text_entry_point_parses_without_a_file(self):
        classes = parse_agent_class_text(BASE_FIXTURE)
        self.assertEqual(sorted(classes), ['Alpha'])
        self.assertEqual(classes['Alpha'].code, 'Alp')


class MacroBoundaryTests(unittest.TestCase):
    '''T7: the documented macro boundary - macros stay per-file.

    The class TEXT is joined, so inheritance resolves across `-I` files; MACRO
    (`<!--ref-->`) expansion does not, because every file is expanded with its own
    macro store. Pinning both directions keeps the documentation and the
    behaviour from drifting apart.
    '''

    MACRO_TEXT = '''
## MacroKid
- **Inherited:** BaseAgent
- **Code:** Mkd
- **Description:** A class whose prompt references a workspace macro.
- **System Prompt:**
    > <!--ref name="snap"-->
- **Preamble:**
    > kid
- **Aftermath:**
    > done
'''

    def tearDown(self):
        for attr in ('workspace', 'extra_dir'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_a_workspace_macro_needs_an_include_not_the_flag(self):
        self.workspace = template_workspace()   # defines `snap` in .yaha/const.md
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_macro_', dir=str(TMP)))
        extra = _write(self.extra_dir / 'macrokid.md', self.MACRO_TEXT)
        agent_class = self.workspace / '.yaha' / 'AgentClass.md'

        # (a) through -I the extra file carries its own macro store: hard error.
        with self.assertRaises(MarkdownLoadError) as caught:
            parse_agent_class_md(str(agent_class), [str(extra)])
        self.assertIn("unknown macro 'snap'", str(caught.exception))

        # (b) through <!--include--> the workspace's macro tree is shared.
        shutil.copy(str(extra), str(self.workspace / '.yaha' / 'macrokid.md'))
        with agent_class.open('a') as handle:
            handle.write('\n\n<!--include path="macrokid.md" -->\n')
        classes = parse_agent_class_md(str(agent_class))
        kid = classes['MacroKid']
        self.assertTrue(kid.system_prompt.strip(), 'inheritance did not apply')
        self.assertNotIn('<!--ref', kid.system_prompt)


if __name__ == '__main__':
    unittest.main()
