#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Workspace crew-environment configuration contract.

Validates THIS workspace's `.crew/environment.md` descriptor and `.crew/protocol.md`
contract: the descriptor schema, the workflow enum plus its per-workflow required keys, that
every declared path exists and stays inside the workspace, that no declared path matches a
path the AccessController denies, and that the lead's `Bootstrap` prompt names the descriptor
skeleton so an agent can author a valid descriptor from scratch.

The descriptor is a property of the WORKSPACE, not of the shipped package, so the tests that
read it SKIP when `.crew/environment.md` is absent; the template-side test always runs.

The crew root is LOCATED, never guessed: `_crew_root()` walks up from this file until it
finds a directory holding `.crew/` (`YAHA_CREW_ROOT` overrides the search). In a tree with no
crew root - notably a canonical checkout, where this module ships - every crew-rooted case
SKIPS by its existing guard, so these tests can never validate an unrelated directory and
report green.
'''
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaha
from yaha.config.agents import parse_agent_class_md

HERE = Path(__file__).resolve().parent


def _crew_root(start):
    '''Locate the crew root: the nearest candidate holding `.crew/`.

    `YAHA_CREW_ROOT` overrides the search. `None` means "there is no crew root
    here", and the caller must SKIP the crew-rooted cases rather than validate
    some unrelated directory.
    '''
    override = os.environ.get('YAHA_CREW_ROOT')
    if override:
        return Path(override)
    for candidate in (start, *start.parents):
        if (candidate / '.crew').is_dir():
            return candidate
    return None


WORKSPACE = _crew_root(HERE)
NO_CREW_ROOT = WORKSPACE is None
if NO_CREW_ROOT:
    # Point the paths at something that cannot exist so the existing guards
    # (`if not DESCRIPTOR.exists(): raise unittest.SkipTest`) skip cleanly. A
    # fixed `HERE.parent.parent` would instead resolve one level ABOVE the repo
    # in a canonical checkout, where this module ships, and could validate the
    # wrong tree while still reporting green.
    WORKSPACE = HERE / '__no_crew_root__'

CREW_DIR = WORKSPACE / '.crew'
DESCRIPTOR = CREW_DIR / 'environment.md'
PROTOCOL = CREW_DIR / 'protocol.md'
TEMPLATE = Path(yaha.__file__).resolve().parent / 'templates' / 'AgentClass.md'

REQUIRED_KEYS = [
    'Name', 'Workflow', 'Main Repo', 'Canonical Root', 'Remote', 'Base Branch',
    'Worktree Root', 'Exchange Folder', 'Merge Record Dir', 'Code Folder',
    'Docs Folder', 'Plans Folder', 'Tests Folder', 'Test Command',
    'VCS Backend', 'Git Dir', 'Merge Target', 'Issue/PR Policy', 'Protocol',
]

WORKFLOWS = ('local-git', 'external-canonical')

# Mirrors core/access.py: the patterns the AccessController refuses to touch.
DENIED_PATTERNS = [r'\.git\b', r'\.env\b', r'\.ssh\b', r'\.DS_Store\b']

KEY_RE = re.compile(r'^- \*\*(.+?):\*\* *(.*)$')


def _descriptor():
    '''Parse the flat `- **Key:** value` descriptor, or skip when absent.'''
    if not DESCRIPTOR.exists():
        raise unittest.SkipTest('no .crew/environment.md in this workspace')
    fields = {}
    for line in DESCRIPTOR.read_text(encoding='utf-8').splitlines():
        match = KEY_RE.match(line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


class DescriptorTests(unittest.TestCase):
    def test_descriptor_has_all_required_fields(self):
        fields = _descriptor()
        missing = [key for key in REQUIRED_KEYS if not fields.get(key)]
        self.assertEqual(missing, [],
                         'every descriptor key must be present and non-empty')

    def test_descriptor_workflow_is_valid_and_complete(self):
        fields = _descriptor()
        self.assertIn(fields['Workflow'], WORKFLOWS)
        if fields['Workflow'] == 'local-git':
            needed = ('Main Repo', 'Worktree Root')
        else:
            needed = ('Canonical Root', 'Worktree Root')
        for key in needed:
            self.assertTrue(fields.get(key),
                            key + ' is required by ' + fields['Workflow'])

    def test_descriptor_paths_exist_and_are_inside_the_workspace(self):
        fields = _descriptor()
        for key in ('Exchange Folder', 'Worktree Root', 'Merge Record Dir',
                    'Code Folder', 'Docs Folder', 'Plans Folder', 'Tests Folder',
                    'Protocol'):
            declared = fields[key]
            path = Path(declared)
            if not path.is_absolute():
                path = WORKSPACE / declared
            self.assertTrue(path.exists(), key + ' does not exist: ' + declared)
            self.assertTrue(
                WORKSPACE == path or WORKSPACE in path.parents,
                key + ' escapes the workspace: ' + declared)
        canonical = Path(fields['Canonical Root'])
        self.assertTrue(canonical.is_absolute(),
                        'Canonical Root must be absolute: ' + fields['Canonical Root'])

    def test_declared_paths_are_access_safe(self):
        fields = _descriptor()
        for key in ('Exchange Folder', 'Worktree Root', 'Merge Record Dir',
                    'Protocol', 'Canonical Root', 'Main Repo'):
            value = fields.get(key) or ''
            for pattern in DENIED_PATTERNS:
                self.assertIsNone(
                    re.search(pattern, value),
                    key + ' matches the denied pattern ' + pattern + ': ' + value)


class ProtocolTests(unittest.TestCase):
    def test_protocol_documents_the_contract(self):
        if not PROTOCOL.exists():
            raise unittest.SkipTest('no .crew/protocol.md in this workspace')
        text = PROTOCOL.read_text(encoding='utf-8')
        fields = _descriptor()
        for token in ('<id>.patch', '<id>.review.md', 'crew.status', 'IN_PROGRESS',
                      'DONE', 'code_ready', 'review_done', 'commit_ready',
                      'commit_done', 'merge.md', 'quit'):
            self.assertIn(token, text)
        for token in (fields['Exchange Folder'], fields['Merge Record Dir']):
            self.assertIn(token, text)


class BootstrapPromptTests(unittest.TestCase):
    def test_template_bootstrap_names_the_descriptor_skeleton(self):
        '''An agent must be able to author a valid descriptor from the prompt.'''
        classes = parse_agent_class_md(str(TEMPLATE))
        crew = classes.get('Crewlda')
        if crew is None:
            # This tree's template has no crew classes yet (a canonical
            # checkout before the crew patch is applied): SKIP, never error.
            raise unittest.SkipTest('no Crewlda class in this template')
        bootstrap = next((stage for stage in crew.prompt_chain
                          if stage['name'] == 'Bootstrap'), None)
        if bootstrap is None:
            raise unittest.SkipTest('no Bootstrap stage in this Crewlda')
        content = bootstrap['content']
        for token in ('.crew/environment.md', '.crew/protocol.md',
                      'local-git', 'external-canonical'):
            self.assertIn(token, content)
        for key in REQUIRED_KEYS:
            self.assertIn('`' + key + '`', content)


class SandboxGitDocTests(unittest.TestCase):
    '''The sandbox `.git` fact must be stated the SAME way in both shipped docs.

    `yaha/docs/crew-environment.md` ships in the canonical package while
    `docs/crew-pipeline.md` is the workspace design note, but both describe the
    same `AccessController` profile, so drift between them is a defect a reader
    cannot resolve - round 3 shipped exactly that contradiction.
    '''

    BANNED = ('fail inside the sandbox', 'denies every')
    REQUIRED = ('re-allow', 'git init', 'git worktree add')

    def _texts(self):
        env = (Path(yaha.__file__).resolve().parent / 'docs'
               / 'crew-environment.md')
        pipeline = WORKSPACE / 'docs' / 'crew-pipeline.md'
        for path in (env, pipeline):
            if not path.exists():
                raise unittest.SkipTest('missing ' + str(path))
        return ((env, env.read_text(encoding='utf-8')),
                (pipeline, pipeline.read_text(encoding='utf-8')))

    def test_both_docs_state_the_same_sandbox_git_fact(self):
        for path, text in self._texts():
            low = text.lower()
            for banned in self.BANNED:
                self.assertNotIn(banned, low,
                                 str(path) + ' restates the old claim: '
                                 + banned)
            for required in self.REQUIRED:
                self.assertIn(required, low,
                              str(path) + ' must state ' + required)


class CrewRootTests(unittest.TestCase):
    '''The crew root must be LOCATED (walk up for `.crew/`), never guessed.

    A positional `HERE.parent.parent` happens to be right in the selfcoding
    layout, but resolves one level ABOVE the repo wherever this module ships
    without a crew workspace, so these cases exist to pin the real rule.
    '''

    def test_crew_root_walks_up_and_honours_the_override(self):
        base = Path(tempfile.mkdtemp(prefix='yaha_crewroot_'))
        try:
            root = base / 'ws'
            (root / '.crew').mkdir(parents=True)
            deep = root / 'a' / 'b' / 'c'
            deep.mkdir(parents=True)
            # the walk-up finds the crew root from any depth
            self.assertEqual(_crew_root(deep), root)
            self.assertEqual(_crew_root(root), root)
            resolved = _crew_root(deep)
            self.assertTrue((resolved / '.crew').is_dir(),
                            'a resolved root must really hold .crew/')
            # the override wins over the walk-up
            other = base / 'other'
            other.mkdir()
            with mock.patch.dict(os.environ, {'YAHA_CREW_ROOT': str(other)}):
                self.assertEqual(_crew_root(deep), other)
            # no `.crew/` anywhere above the tree -> None (guard the host
            # assumption: the temp root itself must have no crew ancestor)
            if not any((parent / '.crew').is_dir() for parent in base.parents):
                self.assertIsNone(_crew_root(base))
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
