import os
import shutil
import tempfile
import unittest

from _template_fixture import TEMPLATE_DIR, install_template

import yaha.cli as cli
from yaha.config.agents import parse_agent_class_md
from yaha.core.engine import YAHAEngine
from yaha.core.soulstore import parse_soul_sections, soul_for_class

NL = chr(10)

LEGACY = '# SOUL' + NL + NL + 'You are Minnie, a helpful and efficient agent.' + NL

SECTIONED = (
    '# SOUL' + NL + NL
    + 'You are Default, a helpful and efficient agent.' + NL + NL
    + '## ChatAgent' + NL
    + 'You are Mina, warm and curious.' + NL + NL
    + '## CodeAgent' + NL
    + 'You are Kris, blunt and precise.' + NL
)

CODE_HEADED = (
    '# SOUL' + NL + NL
    + 'You are Default, a helpful and efficient agent.' + NL + NL
    + '## Cha' + NL
    + 'You are Mina, warm and curious.' + NL
)

class TestSoulSections(unittest.TestCase):
    '''The pure parser/resolver: yaha.core.soulstore.'''

    def test_file_without_sections_is_whole_file(self):
        '''T1: no heading -> the whole text, untouched (backward compat).'''
        self.assertEqual(soul_for_class(LEGACY, 'ChatAgent'), LEGACY)
        self.assertEqual(soul_for_class(LEGACY, 'GhostAgent', 'Gho'), LEGACY)
        self.assertEqual(soul_for_class(LEGACY, None), LEGACY)

    def test_default_preamble_used_when_class_has_no_section(self):
        '''T2: an unmatched class gets the DEFAULT preamble.'''
        default, _ = parse_soul_sections(SECTIONED)
        self.assertEqual(soul_for_class(SECTIONED, 'NoAgent', 'Noa'), default.strip())
        self.assertIn('You are Default', soul_for_class(SECTIONED, 'NoAgent'))

    def test_class_section_selected_by_canonical_name(self):
        '''T3: the class own section, heading included.'''
        chat = soul_for_class(SECTIONED, 'ChatAgent')
        self.assertIn('You are Mina', chat)
        self.assertTrue(chat.startswith('## ChatAgent'))
        self.assertNotIn('Kris', chat)

    def test_name_match_is_case_insensitive(self):
        '''T4: a lower-case heading still matches the class name.'''
        self.assertIn('You are Mina', soul_for_class(SECTIONED, 'chatagent'))
        lowered = SECTIONED.replace('## ChatAgent', '## chatagent')
        self.assertIn('You are Mina', soul_for_class(lowered, 'ChatAgent'))

    def test_short_code_matches_when_name_absent(self):
        '''T5: a `## Cha` heading is found through the class code.'''
        self.assertIn('You are Mina', soul_for_class(CODE_HEADED, 'ChatAgent', 'Cha'))

    def test_name_wins_over_code(self):
        '''T6: name precedence, code as the secondary match.'''
        text = ('# SOUL' + NL + NL + 'You are Default.' + NL + NL
                + '## ChatAgent' + NL + 'You are Mina.' + NL + NL
                + '## Web' + NL + 'You are Sage.' + NL)
        chosen = soul_for_class(text, 'ChatAgent', 'Web')
        self.assertIn('Mina', chosen)
        self.assertNotIn('Sage', chosen)
        self.assertIn('Sage', soul_for_class(text, 'WebSearch', 'Web'))

    def test_no_cross_class_leak(self):
        '''T7: a class never sees another class persona.'''
        chat = soul_for_class(SECTIONED, 'ChatAgent')
        self.assertNotIn('Kris', chat)
        self.assertNotIn('Default', chat)
        kris = soul_for_class(SECTIONED, 'CodeAgent')
        self.assertNotIn('Mina', kris)
        self.assertNotIn('Default', kris)
        ghost = soul_for_class(SECTIONED, 'GhostAgent', 'Gho')
        self.assertNotIn('Mina', ghost)
        self.assertNotIn('Kris', ghost)

    def test_sections_without_preamble_give_empty_soul(self):
        '''T8: sections but no DEFAULT -> an unmatched class gets nothing.'''
        bare = ('## ChatAgent' + NL + 'You are Mina.' + NL + NL
                + '## CodeAgent' + NL + 'You are Kris.' + NL)
        self.assertIn('Mina', soul_for_class(bare, 'ChatAgent'))
        self.assertEqual(soul_for_class(bare, 'GhostAgent', 'Gho'), '')

    def test_three_hash_heading_is_not_a_boundary(self):
        '''T9: `### x` stays inside its section.'''
        text = ('# SOUL' + NL + NL + 'You are Default.' + NL + NL
                + '## ChatAgent' + NL + 'You are Mina.' + NL
                + '### Notes' + NL + 'More Mina.' + NL)
        chat = soul_for_class(text, 'ChatAgent')
        self.assertIn('### Notes', chat)
        self.assertIn('More Mina.', chat)

    def test_duplicate_heading_last_wins(self):
        '''T10: a repeated heading overrides the earlier one.'''
        text = ('# SOUL' + NL + NL + 'You are Default.' + NL + NL
                + '## ChatAgent' + NL + 'First voice.' + NL + NL
                + '## ChatAgent' + NL + 'Second voice.' + NL)
        chat = soul_for_class(text, 'ChatAgent')
        self.assertIn('Second voice.', chat)
        self.assertNotIn('First voice.', chat)

    def test_degenerate_inputs_never_raise(self):
        '''T11: the resolver survives anything the engine can hand it.'''
        for bad in (None, '', 123, b'bytes', ['x']):
            self.assertEqual(soul_for_class(bad, 'ChatAgent'), '')
        # a whitespace-only file is just a heading-less file (R5): verbatim
        self.assertEqual(soul_for_class('   ' + NL, 'ChatAgent'), '   ' + NL)
        self.assertEqual(parse_soul_sections(None), ('', {}))
        self.assertEqual(parse_soul_sections(''), ('', {}))
        self.assertEqual(parse_soul_sections(123), ('', {}))
        self.assertEqual(soul_for_class('## ChatAgent' + NL, 'ChatAgent'), '## ChatAgent')
        self.assertIsInstance(parse_soul_sections('## ' + NL), tuple)

    def test_section_body_preserved_verbatim(self):
        '''T12: internal lines survive; only the edges are trimmed.'''
        text = ('# SOUL' + NL + NL + 'You are Default.' + NL + NL
                + '## ChatAgent' + NL + 'Line one.' + NL + NL
                + '> quoted line' + NL + '    indented' + NL + NL
                + 'Last line.' + NL)
        chat = soul_for_class(text, 'ChatAgent')
        self.assertTrue(chat.startswith('## ChatAgent'))
        self.assertFalse(chat.endswith(NL))
        for fragment in ('Line one.', '> quoted line', '    indented', 'Last line.'):
            self.assertIn(fragment, chat)

    def test_default_helper_for_none_class(self):
        '''T13: soul_for_class(text, None) is what engine.soul_prompt holds.'''
        self.assertEqual(soul_for_class(LEGACY, None), LEGACY)
        default, _ = parse_soul_sections(SECTIONED)
        self.assertEqual(soul_for_class(SECTIONED, None), default.strip())

AGENTCLASS_FIXTURE = (
    NL + '## ChatAgent' + NL
    + '- **Inherited:** None' + NL
    + '- **Code:** Cha' + NL
    + '- **System Prompt:**' + NL
    + '    > chat system prompt' + NL
    + '- **Preamble:**' + NL
    + '    > chat preamble' + NL
    + '- **Aftermath:**' + NL
    + '    > chat done' + NL
    + NL + '## WebSearch' + NL
    + '- **Inherited:** None' + NL
    + '- **Code:** Web' + NL
    + '- **System Prompt:**' + NL
    + '    > web system prompt' + NL
    + '- **Preamble:**' + NL
    + '    > web preamble' + NL
    + '- **Aftermath:**' + NL
    + '    > web done' + NL
    + NL + '## NoAgent' + NL
    + '- **Inherited:** None' + NL
    + '- **Code:** Noa' + NL
    + '- **System Prompt:**' + NL
    + '    > no system prompt' + NL
    + '- **Preamble:**' + NL
    + '    > no preamble' + NL
    + '- **Aftermath:**' + NL
    + '    > no done' + NL
)

SOUL_SECTIONED = (
    '# SOUL' + NL + NL
    + 'You are Default, a helpful and efficient agent.' + NL + NL
    + '## ChatAgent' + NL + 'You are Mina, warm and curious.' + NL + NL
    + '## Web' + NL + 'You are Sage, calm and neutral.' + NL
)

class TestSoulPerClassEngine(unittest.TestCase):
    '''The one wiring point: YAHAEngine._make_agent hands each class a soul.'''

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix='yaha_soul_engine_')
        for sub in ('memory', 'ext', 'logs', 'skills'):
            os.makedirs(os.path.join(self.ws, '.yaha', sub), exist_ok=True)
        self._write('tool-schema.md', '[]')
        self._write('Model.md', '')
        self._write('AgentClass.md', AGENTCLASS_FIXTURE)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _write(self, name, text):
        with open(os.path.join(self.ws, '.yaha', name), 'w', encoding='utf-8') as f:
            f.write(text)

    def _engine(self, soul_text):
        self._write('SOUL.md', soul_text)
        return YAHAEngine(workspace_dir=self.ws)

    def _agent(self, engine, name):
        return engine._make_agent(
            engine.agent_classes[name], caller_id=None, recursion_depth=0)
    def test_two_classes_get_different_souls(self):
        '''T14: the headline behaviour - one file, two identities.'''
        engine = self._engine(SOUL_SECTIONED)
        chat = self._agent(engine, 'ChatAgent')
        web = self._agent(engine, 'WebSearch')
        self.assertIn('You are Mina', chat.soul_prompt)
        self.assertIn('You are Sage', web.soul_prompt)
        self.assertNotEqual(chat.soul_prompt, web.soul_prompt)
        self.assertNotIn('Sage', chat.soul_prompt)
        self.assertNotIn('Mina', web.soul_prompt)

    def test_class_without_section_falls_back_to_default(self):
        '''T15: a class with no section keeps the shared DEFAULT identity.'''
        engine = self._engine(SOUL_SECTIONED)
        nobody = self._agent(engine, 'NoAgent')
        self.assertIn('You are Default', nobody.soul_prompt)
        self.assertNotIn('Mina', nobody.soul_prompt)
        self.assertNotIn('Sage', nobody.soul_prompt)
        self.assertEqual(nobody.soul_prompt, engine.soul_prompt)

    def test_legacy_single_identity_soul_is_unchanged(self):
        '''T16: regression proof for every existing workspace.'''
        engine = self._engine(LEGACY)
        self.assertEqual(engine.soul_prompt, LEGACY)
        for name in ('ChatAgent', 'WebSearch', 'NoAgent'):
            self.assertEqual(self._agent(engine, name).soul_prompt, LEGACY)

    def test_build_system_prompt_starts_with_class_soul(self):
        '''T17: the identity really heads the system prompt.'''
        engine = self._engine(SOUL_SECTIONED)
        agent = self._agent(engine, 'ChatAgent')
        prompt = agent.build_system_prompt()
        self.assertTrue(prompt.startswith(agent.soul_prompt))
        self.assertIn('You are Mina', prompt)

class TestSoulTemplateAndCli(unittest.TestCase):
    '''The shipped template and the CLI entry points it flows through.'''

    def test_shipped_template_keeps_placeholder_before_first_heading(self):
        '''T18: a NEW workspace still gets a DEFAULT identity for every class.'''
        raw = (TEMPLATE_DIR / 'SOUL.md').read_text(encoding='utf-8')
        cut = raw.find(NL + '## ')
        self.assertNotEqual(cut, -1)
        preamble = raw[:cut]
        self.assertIn('{agent_name}', preamble)
        default, sections = parse_soul_sections(raw)
        self.assertTrue(default.strip())
        self.assertIn('exampleclass', sections)
        self.assertIn('anotherclass', sections)
        self.assertNotIn('chatagent', sections)

    def test_shipped_template_examples_name_no_real_class(self):
        '''T18b: the shipped examples must be INERT - no real class name.'''
        raw = (TEMPLATE_DIR / 'SOUL.md').read_text(encoding='utf-8')
        _, sections = parse_soul_sections(raw)
        tmp = tempfile.mkdtemp(prefix='yaha_soul_classes_')
        try:
            install_template(os.path.join(tmp, '.yaha'))
            classes = parse_agent_class_md(os.path.join(tmp, '.yaha', 'AgentClass.md'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        real = set()
        for cfg in classes.values():
            real.add(str(cfg.name).strip().lower())
            real.add(str(cfg.code).strip().lower())
        collision = sorted(set(sections) & real)
        self.assertEqual(collision, [], 'template SOUL examples collide with real classes')

    def test_init_workspace_personalises_and_still_parses(self):
        '''T19: `--init NAME` substitutes the placeholder; sections survive.'''
        old_cwd = os.getcwd()
        tmp = tempfile.mkdtemp(prefix='yaha_soul_init_')
        try:
            os.chdir(tmp)
            cli.init_workspace('TestAgent')
            soul = os.path.join(tmp, '.yaha', 'SOUL.md')
            self.assertTrue(os.path.exists(soul))
            with open(soul, 'r', encoding='utf-8') as f:
                raw = f.read()
            self.assertNotIn('{agent_name}', raw)
            self.assertIn('TestAgent', raw)
            default, sections = parse_soul_sections(raw)
            self.assertTrue(default.strip())
            self.assertIn('exampleclass', sections)
            self.assertIn('anotherclass', sections)
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_upgrade_workspace_keeps_existing_soul(self):
        '''T20: `--upgrade` must never clobber a personalised SOUL.md.'''
        old_cwd = os.getcwd()
        tmp = tempfile.mkdtemp(prefix='yaha_soul_upgrade_')
        try:
            os.chdir(tmp)
            cli.init_workspace('TestAgent')
            soul = os.path.join(tmp, '.yaha', 'SOUL.md')
            mine = ('# SOUL' + NL + NL + 'You are Custom, mine.' + NL + NL
                    + '## ChatAgent' + NL + 'You are Mina.' + NL)
            with open(soul, 'w', encoding='utf-8') as f:
                f.write(mine)
            cli.upgrade_workspace()
            with open(soul, 'r', encoding='utf-8') as f:
                self.assertEqual(f.read(), mine)
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
