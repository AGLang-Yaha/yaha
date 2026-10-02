#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''The venue `/load <path>` command: dynamic Agent Class loading.

`YAHAEngine.load_agent_class_document(path)` gives the interrupt venue the same
power as the CLI `-I/--include` flag, at run time: it re-parses the workspace
`AgentClass.md` plus every accumulated extra file and re-resolves inheritance
from scratch, so the loaded class may inherit from the workspace's classes and
from earlier loaded files.

The one behavioural difference that matters is that the SHARED class map is
mutated IN PLACE: every agent already built by the engine holds the very same
dict object, so a class loaded after those agents exist is immediately visible
to them (and to every later spawn). Reassigning the attribute instead would
desync every live agent.
'''
import shutil
import tempfile
import unittest
from pathlib import Path

from yaha.core.engine import YAHAEngine

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'
TEMPLATES = HOME / 'yaha' / 'templates'


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def workspace_dir():
    '''A temporary workspace directory with the standard .yaha layout.'''
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_venue_load_', dir=str(TMP)))
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


class VenueLoadTests(unittest.TestCase):
    '''T29/T30/T31: dynamic class loading through the venue callback.'''

    def tearDown(self):
        for attr in ('workspace', 'extra_dir'):
            path = getattr(self, attr, None)
            if path is not None:
                shutil.rmtree(str(path), ignore_errors=True)

    def test_load_adds_a_class_in_place(self):
        '''A loaded class is resolvable AND visible to already-built agents.'''
        self.workspace = template_workspace()
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        self.assertIsNone(engine.resolve_agent_class('ExtraClass'))

        # An agent built BEFORE the load holds the SAME dict object.
        shared = engine.agent_classes
        self.assertIsNotNone(engine.resolve_agent_class('BaseAgent'))

        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_venue_extra_', dir=str(TMP)))
        extra = _write(self.extra_dir / 'extra.md', '''
## ExtraClass
- **Inherited:** None
- **Code:** Ext
- **Description:** Loaded through the venue.
- **Model:** DefaultModel
- **System Prompt:**
    > EXTRA PROTOCOL
- **Preamble:**
    > go
- **Aftermath:**
    > done
''')
        report = engine.load_agent_class_document(str(extra))
        self.assertIn('ExtraClass', report)
        self.assertIn('added', report)

        # Resolvable through the engine...
        self.assertIsNotNone(engine.resolve_agent_class('ExtraClass'))
        # ... and through the SHARED map object a live agent would hold.
        self.assertIn('ExtraClass', shared)
        self.assertIs(engine.agent_classes, shared,
                      'the class map must be mutated in place, not reassigned')

    def test_load_missing_path_reports_and_changes_nothing(self):
        self.workspace = template_workspace()
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        before = set(engine.agent_classes)
        report = engine.load_agent_class_document(
            str(self.workspace / 'does-not-exist.md'))
        self.assertIn('not found', report)
        self.assertEqual(set(engine.agent_classes), before)

    def test_load_resolves_inheritance_across_documents(self):
        '''A loaded class may inherit from the workspace classes (like -I).'''
        self.workspace = template_workspace()
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        parent = engine.agent_classes['BaseAgent']

        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_venue_extra_', dir=str(TMP)))
        extra = _write(self.extra_dir / 'child.md', '''
## LoadedChild
- **Inherited:** BaseAgent
- **Code:** Lch
- **Description:** Inherits a workspace class.
- **Toolset:**
    - **Inherited:** All
    - connect_tool
- **Preamble:**
    > child stage
- **Aftermath:**
    > done
''')
        engine.load_agent_class_document(str(extra))
        child = engine.agent_classes['LoadedChild']
        self.assertEqual(child.inherited, 'BaseAgent')
        self.assertEqual(child.model, parent.model)
        self.assertEqual(child.system_prompt, parent.system_prompt)
        self.assertIn('connect_tool', child.toolset)
        self.assertTrue(set(parent.toolset) <= set(child.toolset))

    def test_load_a_file_with_no_classes_adds_nothing(self):
        '''A file that exists but defines no class reports 0 added, no crash.'''
        self.workspace = template_workspace()
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        before = set(engine.agent_classes)
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_venue_extra_', dir=str(TMP)))
        empty = _write(self.extra_dir / 'empty.md', '# just a heading\nno classes here\n')
        report = engine.load_agent_class_document(str(empty))
        self.assertIn('added: 0', report)
        self.assertEqual(set(engine.agent_classes), before)


if __name__ == '__main__':
    unittest.main()