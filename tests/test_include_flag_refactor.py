#!/usr/bin/env python3
'''`-I/--include` loads each class file as its OWN document.

An extra class file is NOT text-joined with the workspace's `AgentClass.md` -
that is what `<!--include-->` does, via macro processing. Instead each file is
loaded and macro-expanded on its own, parsed into its own class map, and only
THEN are the maps MERGED: the primary first, each `-I` file after it, a later
definition winning a name clash WHOLESALE, with inheritance resolved once over
the merged map.

This file pins that boundary, the clash reporting that goes with it, and the CLI
policy (a missing `-I` path is an error at the command line; `--compile` checks a
workspace without running an agent).
'''
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yaha.cli import main as cli_main
from yaha.config import agents as agents_module
from yaha.config.agents import (class_definition_sources,
                                load_agent_classes,
                                parse_agent_class_documents,
                                parse_agent_class_md)
from yaha.core.engine import YAHAEngine
from yaha.core.md_loader import MarkdownLoadError

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _class(name, code=None, description='', inherited='None', model=None,
           system_prompt=None, tools=(), wait=None, body='body'):
    '''One authored class block, in the real `.yaha/AgentClass.md` shape.'''
    lines = ['## %s' % name,
             '- **Inherited:** %s' % inherited,
             '- **Code:** %s' % (code or name[:3].lower())]
    if description:
        lines.append('- **Description:** %s' % description)
    if model:
        lines.append('- **Model:** %s' % model)
    if tools:
        lines.append('- **Toolset:**')
        for tool in tools:
            lines.append('    - %s' % tool)
    if system_prompt:
        lines.append('- **System Prompt:**')
        lines.append('    > %s' % system_prompt)
    lines.append('- **Preamble:**')
    if wait:
        lines.append('    - **Wait Event:** %s' % wait)
    lines.append('    > %s' % body)
    lines.append('- **Aftermath:**')
    lines.append('    > done')
    return '\n'.join(lines) + '\n'


def _dump(config):
    '''Every field of a resolved class, for exact comparisons.'''
    return json.dumps(vars(config), default=str, sort_keys=True)


def _workspace():
    '''A minimal workspace directory (.yaha plus the dirs the engine expects).'''
    TMP.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix='yaha_ifref_', dir=str(TMP)))
    yaha_dir = ws / '.yaha'
    for sub in ('memory', 'ext', 'logs'):
        (yaha_dir / sub).mkdir(parents=True)
    (yaha_dir / 'Model.md').write_text('')
    (yaha_dir / 'tool-schema.md').write_text('[]')
    return ws


class _WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.workspace = _workspace()
        self.extra_dir = Path(tempfile.mkdtemp(prefix='yaha_extras_', dir=str(TMP)))

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.workspace, ignore_errors=True)
        shutil.rmtree(self.extra_dir, ignore_errors=True)

    @property
    def agent_class(self):
        return self.workspace / '.yaha' / 'AgentClass.md'


class DocumentBoundaryTests(_WorkspaceTest):
    '''T1: two documents stay two documents.'''

    def test_text_before_an_extra_documents_first_heading_cannot_bleed(self):
        primary = _write(self.agent_class,
                         _class('Alpha', description='the primary class'))
        extra = _write(self.extra_dir / 'extra.md',
                       'STRAY TEXT THAT MUST NOT LEAK\n\n'
                       + _class('Extra', description='the extra class'))
        alone = parse_agent_class_md(str(primary))
        both = parse_agent_class_md(str(primary), [str(extra)])
        self.assertEqual(_dump(alone['Alpha']), _dump(both['Alpha']))
        self.assertNotIn('STRAY TEXT', _dump(both['Alpha']))

    def test_both_documents_contribute_their_classes(self):
        _write(self.agent_class, _class('Alpha'))
        extra = _write(self.extra_dir / 'extra.md', _class('Extra'))
        both = parse_agent_class_md(str(self.agent_class), [str(extra)])
        self.assertEqual(sorted(both), ['Alpha', 'Extra'])


class CrossDocumentInheritanceTests(_WorkspaceTest):
    '''T2: inheritance resolves over the MERGED map, in order.'''

    def test_a_chain_spans_documents(self):
        _write(self.agent_class,
               _class('Base', description='the base', model='DefaultModel',
                      system_prompt='BASE PROTOCOL', tools=['bash_tool']))
        mid = _write(self.extra_dir / 'a_mid.md', _class('Mid', inherited='Base'))
        leaf = _write(self.extra_dir / 'b_leaf.md', _class('Leaf', inherited='Mid'))
        classes = parse_agent_class_md(str(self.agent_class), [str(mid), str(leaf)])
        resolved = classes['Leaf']
        self.assertEqual(resolved.inherited, 'Mid')
        self.assertIn('BASE PROTOCOL', resolved.system_prompt)
        self.assertIn('bash_tool', resolved.toolset)
        self.assertEqual(resolved.model, classes['Base'].model)

    def test_a_later_definition_of_the_same_name_wins(self):
        _write(self.agent_class, _class('Target', description='from primary'))
        extra = _write(self.extra_dir / 'later.md',
                       _class('Target', description='from extra'))
        classes = parse_agent_class_md(str(self.agent_class), [str(extra)])
        self.assertEqual(classes['Target'].description, 'from extra')


class MacroScopeTests(_WorkspaceTest):
    '''T3: macros stay per-DOCUMENT, in both directions.'''

    MACRO = ('<!-- define name="mine", params=[] -->\nINSIDE THE MACRO\n'
             '<!-- enddefine -->\n')

    def test_an_extra_document_may_use_its_own_macro(self):
        _write(self.agent_class, _class('Alpha'))
        extra = _write(self.extra_dir / 'own.md',
                       self.MACRO + _class('Kid', system_prompt='<!--ref name="mine"-->'))
        classes = parse_agent_class_md(str(self.agent_class), [str(extra)])
        self.assertIn('INSIDE THE MACRO', classes['Kid'].system_prompt)

    def test_a_document_cannot_use_a_macro_defined_in_the_other(self):
        _write(self.agent_class, self.MACRO)
        extra = _write(self.extra_dir / 'ref.md',
                       _class('Kid', system_prompt='<!--ref name="mine"-->'))
        with self.assertRaises(MarkdownLoadError):
            parse_agent_class_md(str(self.agent_class), [str(extra)])

    def test_the_same_holds_the_other_way_round(self):
        _write(self.agent_class, _class('Alpha', system_prompt='<!--ref name="mine"-->'))
        extra = _write(self.extra_dir / 'own.md', self.MACRO + _class('Kid'))
        with self.assertRaises(MarkdownLoadError):
            parse_agent_class_md(str(self.agent_class), [str(extra)])


class ConflictReportingTests(_WorkspaceTest):
    '''T4: duplicates and clashes are REPORTED, not silently collapsed.'''

    def test_an_internal_duplicate_is_counted_and_the_last_wins(self):
        doc = _write(self.agent_class,
                     _class('Dup', description='first') + '\n'
                     + _class('Dup', description='second'))
        merged, sources = parse_agent_class_documents([str(doc)])
        self.assertEqual(sources['Dup'], [(str(doc), 2)])
        self.assertEqual(merged['Dup'].description, 'second')

    def test_a_cross_document_clash_preserves_order_and_counts(self):
        primary = _write(self.agent_class,
                         _class('A') + '\n' + _class('B', description='from primary'))
        extra = _write(self.extra_dir / 'extra.md',
                       _class('B', description='from extra') + '\n' + _class('C'))
        merged, sources = parse_agent_class_documents([str(primary), str(extra)])
        self.assertEqual(sources['A'], [(str(primary), 1)])
        self.assertEqual(sources['B'], [(str(primary), 1), (str(extra), 1)])
        self.assertEqual(sources['C'], [(str(extra), 1)])
        self.assertEqual(merged['B'].description, 'from extra')

    def test_a_name_defined_three_times_is_counted_exactly(self):
        primary = _write(self.agent_class, _class('T', description='one'))
        extra = _write(self.extra_dir / 'extra.md',
                       _class('T', description='two') + '\n'
                       + _class('T', description='three'))
        merged, sources = parse_agent_class_documents([str(primary), str(extra)])
        self.assertEqual(sum(count for _path, count in sources['T']), 3)
        self.assertEqual(merged['T'].description, 'three')

    def test_the_public_helper_matches_the_documents_api(self):
        primary = _write(self.agent_class, _class('A'))
        extra = _write(self.extra_dir / 'extra.md', _class('A'))
        sources = class_definition_sources(str(primary), [str(extra)])
        self.assertEqual(sources['A'], [(str(primary), 1), (str(extra), 1)])


class EngineDiagnosticsTests(_WorkspaceTest):
    '''T5: the engine exposes the diagnostics without changing `run`.'''

    def test_a_clash_across_files_is_collected_and_the_class_still_resolves(self):
        _write(self.agent_class, _class('Alpha', description='from primary'))
        extra = _write(self.extra_dir / 'extra.md',
                       _class('Alpha', description='from extra'))
        engine = YAHAEngine(workspace_dir=str(self.workspace),
                            include_agent_files=[str(extra)])
        self.assertIn('Alpha', engine.class_conflicts)
        entries = engine.class_conflicts['Alpha']
        self.assertEqual(sum(count for _path, count in entries), 2)
        self.assertEqual(engine.agent_classes['Alpha'].description, 'from extra')

    def test_an_unknown_parent_is_reported(self):
        _write(self.agent_class, _class('Orphan', inherited='Missing'))
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        self.assertIn(('Orphan', 'Missing'), engine.unknown_parents)

    def test_a_clean_workspace_has_no_conflicts_and_no_unknown_parents(self):
        _write(self.agent_class, _class('Solo'))
        engine = YAHAEngine(workspace_dir=str(self.workspace))
        self.assertEqual(engine.class_conflicts, {})
        self.assertEqual(engine.unknown_parents, [])


class CliPolicyTests(_WorkspaceTest):
    '''T6: the CLI's strict paths and the `--compile` check mode.'''

    def _run(self, argv):
        buffer = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buffer):
            try:
                with mock.patch('sys.argv', ['run-yaha'] + list(argv)):
                    cli_main()
            except SystemExit as exc:
                code = exc.code
        return code, buffer.getvalue()

    def test_a_missing_include_path_fails_at_the_command_line(self):
        code, out = self._run(['-w', str(self.workspace), '-I',
                               str(self.extra_dir / 'nope.md'), '--compile'])
        self.assertEqual(code, 2)
        self.assertIn('included Agent Class file not found', out)

    def test_compile_reports_a_clean_workspace(self):
        _write(self.agent_class, _class('Alpha'))
        code, out = self._run(['-w', str(self.workspace), '--compile'])
        self.assertIsNone(code)
        self.assertIn('Checked', out)
        self.assertIn('OK: 0 errors', out)

    def test_compile_reports_a_producer_less_wait_event_as_an_error(self):
        _write(self.agent_class, _class('Waiting', wait='never_signalled'))
        code, out = self._run(['-w', str(self.workspace), '--compile'])
        self.assertEqual(code, 1)
        self.assertIn('never_signalled', out)

    def test_compile_reports_a_clash_as_a_warning_with_exit_zero(self):
        _write(self.agent_class, _class('Alpha', description='from primary'))
        extra = _write(self.extra_dir / 'extra.md',
                       _class('Alpha', description='from extra'))
        code, out = self._run(['-w', str(self.workspace), '-I', str(extra),
                               '--compile'])
        self.assertIsNone(code)
        self.assertIn('Warning:', out)
        self.assertIn('Alpha', out)

    def test_compile_reports_an_unknown_parent_as_an_error(self):
        _write(self.agent_class, _class('Orphan', inherited='Missing'))
        code, out = self._run(['-w', str(self.workspace), '--compile'])
        self.assertEqual(code, 1)
        self.assertIn('Missing', out)


class HelpTextTests(unittest.TestCase):
    '''T7: `yaha --help` must describe the CURRENT `-I` semantics.'''

    def setUp(self):
        self.source = (HOME / 'yaha' / 'cli.py').read_text()

    def test_the_replaced_text_join_wording_is_gone(self):
        self.assertNotIn('resolved like an <!--include-->', self.source)
        self.assertNotIn('their class text', self.source)

    def test_the_help_describes_documents_merging_and_strictness(self):
        self.assertIn('its own document', self.source)
        self.assertIn('merged with the', self.source)
        self.assertIn('does not exist is an error', self.source)
        self.assertIn('--compile', self.source)


class SingleParseTests(_WorkspaceTest):
    '''T8/T9: one read per document, and one source of truth for both results.'''

    def test_each_document_is_loaded_exactly_once_per_engine(self):
        _write(self.agent_class, _class('Alpha'))
        extra = _write(self.extra_dir / 'extra.md', _class('Extra'))
        calls = []
        real = agents_module.load_md_with_macros

        def recording(path, *args, **kwargs):
            calls.append(str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(agents_module, 'load_md_with_macros', recording):
            engine = YAHAEngine(workspace_dir=str(self.workspace),
                                include_agent_files=[str(extra)])
        self.assertEqual(calls.count(str(self.agent_class)), 1)
        self.assertEqual(calls.count(str(extra)), 1)
        self.assertTrue(engine.class_sources)
        self.assertEqual(engine.class_conflicts, {})

    def test_load_agent_classes_is_the_single_source_of_truth(self):
        primary = _write(self.agent_class, _class('X', description='from primary'))
        extra = _write(self.extra_dir / 'extra.md',
                       _class('X', description='from extra'))
        classes, sources = load_agent_classes(str(primary), [str(extra)])
        self.assertEqual(classes['X'].description, 'from extra')
        self.assertEqual(sources['X'], [(str(primary), 1), (str(extra), 1)])
        wrapped = parse_agent_class_md(str(primary), [str(extra)])
        self.assertEqual(_dump(wrapped['X']), _dump(classes['X']))
        self.assertEqual(class_definition_sources(str(primary), [str(extra)]),
                         sources)


if __name__ == '__main__':
    unittest.main()
