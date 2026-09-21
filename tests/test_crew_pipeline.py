#!/usr/bin/env python3
'''Crew coder-reviewer pipeline: contract + end-to-end tests.

Contract tests assert that the three Crew AgentClasses in the shipped
templates/AgentClass.md are wired for a MULTI-ROUND run: the coder Summary
ALWAYS loops and never finishes, the coder Aftermath is the shutdown stage that
writes crew.status=DONE and pulses code_ready, the reviewer exits only on an
exact DONE marker and otherwise loops for the next round, and the shipped
classes stay untouched.

The integration tests run the REAL classes through the multi-agency runtime
with a stage-aware fake provider and prove: three coding/review rounds (two
revision rounds then an approval), that every round really BLOCKS (each review
embeds the patch of ITS round, so a stale handshake would be caught), that
APPROVE does NOT end the crew, that a stale stop marker cannot make the
reviewer exit early, and that a quit at the coder Preamble ends the whole crew
- coder and reviewer - without a deadlock.
'''
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaha
from _template_fixture import install_template
from yaha.config.agents import parse_agent_class_md
from yaha.core.engine import YAHAEngine
from yaha.core.runtime import validate_wait_events

HOME = Path(__file__).resolve().parent.parent
TMP = HOME / 'tmp'
TEMPLATE = Path(yaha.__file__).resolve().parent / 'templates' / 'AgentClass.md'

PATCH_NAME = 'crew_change.patch'
REVIEW_NAME = 'crew_change.review.md'
STATUS_NAME = 'crew.status'

# The descriptor's canonical tree lives OUTSIDE this workspace, so a host
# command aimed at it is never whitelisted (`AccessController.bash_whitelist`
# holds only bare readers) and the unattended ACS question answers with its
# default and denies. The command is `--dry-run`: harmless even if it ever ran.
CANONICAL_ROOT = '/Users/kuntan/code/claud/yaha-lite'

_LT = chr(60)
_GT = chr(62)
_TOOL_OPEN = _LT + 'tool_code' + _GT
_TOOL_CLOSE = _LT + '/tool_code' + _GT


def _def_stage(name, chain):
    return next((s for s in chain if s['name'] == name), None)


def _classes():
    return parse_agent_class_md(str(TEMPLATE))


class CrewldaContractTests(unittest.TestCase):
    def test_chain_agentset_and_privilege_cap(self):
        classes = _classes()
        cfg = classes['Crewlda']
        self.assertEqual([s['name'] for s in cfg.prompt_chain],
                         ['Preamble', 'Bootstrap', 'Fan-out', 'Commit', 'Rearm',
                          'Aftermath'])
        self.assertIn('CrewCda', cfg.agentset)
        self.assertIn('CrewRva', cfg.agentset)
        self.assertIn('spawn_parallel_agent', cfg.toolset)
        for peer in ('CrewCda', 'CrewRva'):
            target = classes[peer]
            missing = [t for t in target.toolset if t not in cfg.toolset]
            self.assertEqual(missing, [],
                             'lead must stay a toolset superset of ' + peer)

    def test_fanout_names_both_peers_and_events(self):
        fan = _def_stage('Fan-out', _classes()['Crewlda'].prompt_chain)
        self.assertIn('spawn_parallel_agent', fan['allowed_tools'])
        self.assertIn('CrewCda', fan['content'])
        self.assertIn('CrewRva', fan['content'])
        self.assertIn("set_event_success('', 'code_ready')", fan['content'])
        self.assertIn("set_event_loop(summary='', names=['review_done'])", fan['content'])
        for token in ('round', 'DONE', 'IN_PROGRESS', 'REQUEST_CHANGES'):
            self.assertIn(token, fan['content'])

    def test_fanout_carries_the_descriptor(self):
        fan = _def_stage('Fan-out', _classes()['Crewlda'].prompt_chain)
        for token in ('.crew/environment.md', 'Workflow', 'Worktree Root',
                      'Exchange Folder', 'Test Command', 'VCS Backend'):
            self.assertIn(token, fan['content'])

    def test_bootstrap_reads_or_derives_one_fixed_environment(self):
        boot = _def_stage('Bootstrap', _classes()['Crewlda'].prompt_chain)
        self.assertIn('create_file', boot['allowed_tools'])
        content = boot['content']
        for token in ('.crew/environment.md', '.crew/protocol.md',
                      'local-git', 'external-canonical',
                      'NEVER rewrite', 'IN_PROGRESS'):
            self.assertIn(token, content)
        for key in ('Name', 'Workflow', 'Main Repo', 'Canonical Root', 'Remote',
                    'Base Branch', 'Worktree Root', 'Exchange Folder',
                    'Merge Record Dir', 'Code Folder', 'Docs Folder',
                    'Plans Folder', 'Tests Folder', 'Test Command',
                    'VCS Backend', 'Git Dir', 'Merge Target',
                    'Issue/PR Policy', 'Protocol'):
            self.assertIn('`' + key + '`', content)

    def test_commit_gate_is_gated_and_reentrant(self):
        commit = _def_stage('Commit', _classes()['Crewlda'].prompt_chain)
        self.assertEqual(commit.get('wait_event'), 'commit_ready')
        self.assertEqual(sorted(commit['exit_tools']),
                         ['exit_to', 'set_event_success'])
        self.assertNotIn('set_event_loop', commit['exit_tools'])
        # `ask_user` cannot be a REQUIRED tool: on the final pass there may be
        # nothing to merge, and the template itself sends that case straight to
        # Step 6 without prompting. It must however be mandated in prose.
        self.assertIn('ask_user', commit.get('allowed_tools') or [])
        content = commit['content']
        for token in ('APPROVE', 'ask_user', 'local-git', 'external-canonical',
                      "set_event_success('', 'commit_done')", "exit_to('Aftermath')",
                      'Merge Record Dir', 'NEVER write `crew.status`',
                      'NEVER act without the user'):
            self.assertIn(token, content)
        # The VCS-denied fallback: report the exact host commands, never force.
        self.assertIn('EXACT command', content)

    def test_commit_denial_mandates_exact_host_commands(self):
        '''A denied VCS action is a NORMAL outcome, never a stranded coder.'''
        content = _def_stage('Commit', _classes()['Crewlda'].prompt_chain)['content']
        self.assertIn('DENIED', content)
        self.assertIn('do NOT force it and do NOT retry it', content)
        self.assertIn('EXACT command(s)', content)
        self.assertIn('the host must still run after a denial', content)
        self.assertIn('NEVER skips this step', content)
        self.assertIn("set_event_success('', 'commit_done')", content)
        self.assertIn("exit_to('Aftermath')", content)

    def test_rearm_only_jumps_back(self):
        chain = _classes()['Crewlda'].prompt_chain
        names = [s['name'] for s in chain]
        self.assertEqual(names[names.index('Commit') + 1], 'Rearm')
        rearm = _def_stage('Rearm', chain)
        self.assertEqual(rearm['exit_tools'], ['exit_to'])
        self.assertIn("exit_to('Commit')", rearm['content'])

    def test_no_commit_ready_signal_via_set_event_loop(self):
        '''F4 guard: the spawn/System prompts must not contradict Summary.

        `set_event_loop` is not even an allowed tool in CrewCda.Summary, so a
        prompt telling the coder to use it there would error out - or, worse,
        re-enter the interactive Preamble instead of parking in CommitWait,
        defeating the whole input-lock guard.
        '''
        blob = ''
        for cfg in _classes().values():
            blob += cfg.system_prompt or ''
            for stage in cfg.prompt_chain:
                blob += stage.get('content') or ''
        self.assertNotIn("set_event_loop('', 'commit_ready')", blob)

    def test_event_producers_and_waiters(self):
        '''Explicit wiring: who produces each event and who waits on it.'''
        classes = _classes()
        self.assertEqual(validate_wait_events(classes), [])
        waiters, producers = {}, {}
        for name, cfg in classes.items():
            for stage in cfg.prompt_chain:
                if stage.get('wait_event'):
                    waiters.setdefault(stage['wait_event'], set()).add(
                        name + '.' + stage['name'])
                body = (cfg.system_prompt or '') + (stage.get('content') or '')
                for event in ('code_ready', 'review_done', 'commit_ready',
                              'commit_done'):
                    # The shipped spellings: set_event_success keeps the empty
                    # summary positional; set_event_loop uses the canonical
                    # keyword form (summary + names list).
                    if ("set_event_success('', '" + event + "')" in body
                            or ("set_event_loop(summary='', names=['"
                                + event + "'])" in body)):
                        producers.setdefault(event, set()).add(
                            name + '.' + stage['name'])
        self.assertIn('CrewCda.Coding', producers['code_ready'])
        self.assertIn('CrewCda.Aftermath', producers['code_ready'])
        self.assertIn('CrewRva.Review', waiters['code_ready'])
        self.assertIn('CrewRva.Review', producers['review_done'])
        self.assertIn('CrewCda.Summary', waiters['review_done'])
        self.assertIn('Crewlda.Commit', waiters['commit_ready'])
        self.assertIn('CrewCda.Summary', producers['commit_ready'])
        self.assertIn('Crewlda.Commit', producers['commit_done'])
        self.assertIn('CrewCda.CommitWait', waiters['commit_done'])
        # The lead cannot release the reviewer: its ONLY event-signalling stage
        # is Commit, and that one signals commit_done. Fan-out merely DESCRIBES
        # the coder's contract inside its spawn prompt.
        lead_signalling = {s['name'] for s in classes['Crewlda'].prompt_chain
                           if 'set_event_success' in (s.get('exit_tools') or [])}
        self.assertEqual(lead_signalling, {'Commit'})


class CrewCdaContractTests(unittest.TestCase):
    def test_chain(self):
        cfg = _classes()['CrewCda']
        self.assertEqual([s['name'] for s in cfg.prompt_chain],
                         ['Preamble', 'Plan', 'Approve', 'Coding', 'Summary',
                          'CommitWait', 'Aftermath'])

    def test_coding_publishes_each_round_with_hygiene(self):
        coding = _def_stage('Coding', _classes()['CrewCda'].prompt_chain)
        self.assertNotIn('ask_user', coding.get('required_tools') or [])
        self.assertNotIn('ask_user', coding.get('allowed_tools') or [])
        self.assertEqual(coding.get('exit_tools'), ['set_event_success'])
        for token in ("set_event_success('', 'code_ready')", STATUS_NAME,
                      'IN_PROGRESS', 'SAME'):
            self.assertIn(token, coding['content'])
        self.assertNotIn('ask_user', coding['content'])

    def test_summary_always_jumps_back_and_never_finishes(self):
        summary = _def_stage('Summary', _classes()['CrewCda'].prompt_chain)
        self.assertEqual(summary.get('wait_event'), 'review_done')
        self.assertEqual(summary['exit_tools'], ['exit_to', 'set_event_success'])
        for tool in ('success_tool', 'fail_tool'):
            self.assertNotIn(tool, summary['exit_tools'])
            self.assertNotIn(tool, summary.get('allowed_tools') or [])
        for token in ('exit_to', 'REQUEST_CHANGES', 'APPROVE', '.review.md',
                      'commit_ready', 'quit'):
            self.assertIn(token, summary['content'])

    def test_commit_wait_parks_the_coder(self):
        '''The TUI input-lock guard: the coder PARKS, it never finishes.'''
        wait = _def_stage('CommitWait', _classes()['CrewCda'].prompt_chain)
        self.assertEqual(wait.get('wait_event'), 'commit_done')
        self.assertEqual(wait.get('exit_tools'), ['exit_to'])
        for tool in ('success_tool', 'fail_tool', 'set_event_success', 'loop_tool'):
            self.assertNotIn(tool, wait.get('exit_tools') or [])
            self.assertNotIn(tool, wait.get('allowed_tools') or [])
        self.assertIn('merge', wait['content'].lower())

    def test_aftermath_is_the_shutdown_stage(self):
        aftermath = _def_stage('Aftermath', _classes()['CrewCda'].prompt_chain)
        self.assertEqual(aftermath.get('exit_tools'), ['set_event_success'])
        self.assertIn('create_file', aftermath.get('allowed_tools') or [])
        for token in (STATUS_NAME, 'DONE', "set_event_success('', 'code_ready')"):
            self.assertIn(token, aftermath['content'])

    def test_coder_rounds_are_unbounded(self):
        cfg = _classes()['CrewCda']
        self.assertEqual(cfg.max_loops, -1)

    def test_preamble_is_untouched_so_the_quit_path_is_inherited(self):
        coder = _def_stage('Preamble', _classes()['CrewCda'].prompt_chain)
        base = _def_stage('Preamble', _classes()['CodeAgent'].prompt_chain)
        self.assertEqual(coder['allowed_tools'], base['allowed_tools'])
        self.assertEqual(coder['required_tools'], base['required_tools'])
        self.assertEqual(sorted(coder['exit_tools']), sorted(base['exit_tools']))
        self.assertEqual(len(coder['content']), len(base['content']))

    def test_approve_keeps_the_human_gate(self):
        approve = _def_stage('Approve', _classes()['CrewCda'].prompt_chain)
        self.assertIn('ask_user', approve['required_tools'])
        self.assertIn('ask_user', approve['allowed_tools'])


class CrewRvaContractTests(unittest.TestCase):
    def test_chain_and_wait(self):
        review = _def_stage('Review', _classes()['CrewRva'].prompt_chain)
        self.assertEqual(review.get('wait_event'), 'code_ready')
        self.assertEqual(review['exit_tools'],
                         ['set_event_loop', 'set_event_success'])

    def test_reviewer_is_unattended(self):
        cfg = _classes()['CrewRva']
        self.assertNotIn('ask_user', cfg.toolset)
        blob = cfg.system_prompt or ''
        for stage in cfg.prompt_chain:
            blob += chr(10) + (stage.get('content') or '')
            for key in ('allowed_tools', 'required_tools', 'exit_tools'):
                self.assertNotIn('ask_user', stage.get(key) or [], stage['name'])
        self.assertNotIn('ask_user', blob)

    def test_review_writes_next_to_patch_and_uses_events(self):
        review = _def_stage('Review', _classes()['CrewRva'].prompt_chain)
        self.assertIn('.review.md', review['content'])
        self.assertIn("set_event_loop(summary='', names=['review_done'])", review['content'])
        self.assertIn(STATUS_NAME, review['content'])

    def test_review_exits_only_on_an_exact_done_marker(self):
        review = _def_stage('Review', _classes()['CrewRva'].prompt_chain)
        content = review['content']
        self.assertIn('EXACTLY `DONE`', content)
        self.assertIn('IN_PROGRESS', content)
        self.assertIn("set_event_success('', 'commit_ready')", content)
        self.assertIn('set_event_loop', review['exit_tools'])


class ProducerValidationTests(unittest.TestCase):
    def test_shipped_template_has_no_missing_producers(self):
        self.assertEqual(validate_wait_events(_classes()), [])

    def test_missing_producer_is_reported(self):
        TMP.mkdir(parents=True, exist_ok=True)
        fixture = TMP / 'crew_missing_producer.md'
        fixture.write_text(
            '## W' + chr(10) +
            '- **Inherited:** None' + chr(10) +
            '- **Code:** Www' + chr(10) +
            '- **Model:** m' + chr(10) +
            '- **Preamble:**' + chr(10) +
            '    > go' + chr(10) +
            '- **Work:**' + chr(10) +
            '    - **Wait Event:** never_set' + chr(10) +
            '    > work' + chr(10) +
            '- **Aftermath:**' + chr(10) +
            '    > done' + chr(10), encoding='utf-8')
        self.assertEqual(
            validate_wait_events(parse_agent_class_md(str(fixture))),
            ['never_set'])


class ShippedClassesUntouchedTests(unittest.TestCase):
    def test_codeagent_coding_still_requires_approval(self):
        coding = _def_stage('Coding', _classes()['CodeAgent'].prompt_chain)
        self.assertIn('ask_user', coding['required_tools'])

    def test_codereviewer_still_writes_next_to_the_patch(self):
        cfg = _classes()['CodeReviewer']
        self.assertTrue(any('.review.md' in (s.get('content') or '')
                            for s in cfg.prompt_chain))


def _tool(name, args):
    payload = json.dumps({'tool': name, 'args': args})
    return _TOOL_OPEN + payload + _TOOL_CLOSE


_MARKERS = [
    'CREW LEAD - INTAKE PHASE',
    'CREW LEAD - BOOTSTRAP PHASE',
    'CREW LEAD - FAN-OUT PHASE',
    'CREW LEAD - COMMIT PHASE',
    'CREW LEAD - REARM PHASE',
    'CodeAgent Preamble phase',
    'CODEAGENT PLAN PHASE',
    'CODEAGENT APPROVE PHASE',
    'CREW CODER - CODING PHASE',
    'CREW CODER - SUMMARY PHASE',
    'CREW CODER - COMMIT WAIT PHASE',
    'CREW REVIEWER - INTAKE PHASE',
    'CREW REVIEWER - REVIEW PHASE',
    'CREW CODER - SHUTDOWN PHASE',
    'Crew lead finished',
    'Crew reviewer finished',
]


def _current_stage(messages):
    found = None
    for message in messages:
        content = message.get('content') or ''
        for marker in _MARKERS:
            if marker in content:
                found = marker
    return found


class _NonTTY:
    def isatty(self):
        return False

    def readline(self):
        return ''

    def fileno(self):
        raise OSError('no fileno')


_SCRIPT = {'exchange': '', 'approve_after': 3, 'quit_before_coding': False,
           'visits': {}, 'reviewed': [], 'markers_seen': [],
           'order': [], 'commit_after_round': [], 'gates': [],
           'merge_answer': 'approve', 'merge_records': [],
           'quit_after_preamble': 3, 'vcs_denied': False,
           'host_cmd': '', 'denial_seen': None, 'merge_path': '',
           'done_relays': 0}


def _entries(marker):
    return _SCRIPT['visits'].get(marker, 0)


def _note_stage(marker):
    '''Record every stage entry, in real cross-agent (thread) order.

    The TUI input-lock guard is an ORDERING property - the coder must park in
    CommitWait BEFORE the lead prompts for the merge - so the tests need the
    real interleaving, not just per-stage counters.
    '''
    _SCRIPT['order'].append(marker)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _last_tool_result(messages):
    '''Return the newest TOOL RESULT the agent has been handed, if any.'''
    for message in reversed(messages):
        content = message.get('content') or ''
        if content.startswith('TOOL RESULT ('):
            return content
    return ''


def _patch_round(exchange):
    '''Report the round tag of the patch the coder last published.'''
    patch = Path(exchange) / PATCH_NAME
    if not patch.exists():
        return 'NO PATCH'
    for line in patch.read_text(encoding='utf-8').splitlines():
        if line.startswith('+round '):
            return line[1:]
    return 'UNKNOWN'


class _CrewProvider:
    '''Stage-aware fake provider playing coder, reviewer and human.

    One instance per agent, so `_turn` is per-agent; the cross-agent protocol
    state (round counters, what each review saw) lives in `_SCRIPT`.
    '''
    usage = {'prompt_tokens': 1, 'completion_tokens': 1}
    exchange = None

    def __init__(self, cfg=None):
        self.calls = 0
        self._last = None
        self._spawns = 0
        self._turn = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        marker = _current_stage(messages)
        result = _last_tool_result(messages)
        if 'Access Denied' in result or 'Security Block' in result:
            _SCRIPT['denial_seen'] = result
        if marker != self._last:
            self._last = marker
            _SCRIPT['visits'][marker] = _SCRIPT['visits'].get(marker, 0) + 1
            _note_stage(marker)
            self._turn = 0
        self._turn += 1
        return self._respond(marker), dict(self.usage), None

    def _respond(self, marker):
        exchange = Path(self.exchange)
        if marker == 'CREW LEAD - INTAKE PHASE':
            return _tool('success_tool', {'summary': 'task + exchange folder noted'})
        if marker == 'CREW LEAD - BOOTSTRAP PHASE':
            if self._turn == 1:
                return _tool('create_file', {
                    'path': str(exchange.parent / '.crew' / 'environment.md'),
                    'content': 'Workflow: external-canonical'})
            if self._turn == 2:
                return _tool('create_file', {
                    'path': str(exchange / STATUS_NAME),
                    'content': 'IN_PROGRESS'})
            return _tool('success_tool', {'summary': 'descriptor pinned'})
        if marker == 'CREW LEAD - FAN-OUT PHASE':
            if self._spawns == 0:
                self._spawns = 1
                return _tool('spawn_parallel_agent', {
                    'agent_class': 'CrewCda', 'clone_context': 'new',
                    'prompt': 'Implement the change. Shared exchange folder: '
                              + str(exchange)})
            if self._spawns == 1:
                self._spawns = 2
                return _tool('spawn_parallel_agent', {
                    'agent_class': 'CrewRva', 'clone_context': 'new',
                    'prompt': 'Review the newest patch. Shared exchange folder: '
                              + str(exchange)})
            return _tool('success_tool', {'summary': 'both peers spawned'})
        if marker == 'CREW LEAD - COMMIT PHASE':
            status_file = exchange / STATUS_NAME
            status = (status_file.read_text(encoding='utf-8').strip()
                      if status_file.exists() else '')
            if self._turn == 1:
                _SCRIPT['gates'].append({'status': status,
                                         'order': len(_SCRIPT['order'])})
                if status == 'DONE':
                    return _tool('exit_to', {'name': 'Aftermath'})
                return _tool('ask_user', {
                    'question': 'Merge the approved patch into the canonical '
                                'tree? (merge gate)'})
            if _SCRIPT['vcs_denied']:
                if self._turn == 2:
                    # Run the approved action for REAL: the SANDBOX decides.
                    return _tool('bash_tool', {'cmdline': _SCRIPT['host_cmd']})
                if self._turn == 3:
                    _SCRIPT['merge_records'].append('denied')
                    return _tool('create_file', {
                        'path': _SCRIPT['merge_path'],
                        'content': 'outcome: denied' + chr(10)
                                   + 'host commands:' + chr(10)
                                   + _SCRIPT['host_cmd'] + chr(10)})
                return _tool('set_event_success', {'name': 'commit_done'})
            if self._turn == 2:
                answer = _SCRIPT['merge_answer']
                _SCRIPT['merge_records'].append(answer)
                return _tool('create_file', {
                    'path': str(exchange / 'merge-record.md'),
                    'content': 'outcome: ' + answer})
            return _tool('set_event_success', {'name': 'commit_done'})
        if marker == 'CREW LEAD - REARM PHASE':
            return _tool('exit_to', {'name': 'Commit'})
        if marker == 'CodeAgent Preamble phase':
            quit_now = _SCRIPT['quit_before_coding'] and self._turn == 1
            if quit_now or _entries(marker) > _SCRIPT['quit_after_preamble']:
                return _tool('fail_tool', {'reason': 'user said quit'})
            if self._turn == 1:
                return _tool('ask_user', {'question': 'Any change to the task?'})
            return _tool('success_tool', {'summary': 'task accepted'})
        if marker == 'CODEAGENT PLAN PHASE':
            if self._turn == 1:
                return _tool('create_file', {
                    'path': str(exchange / 'plan.md'),
                    'content': 'the plan'})
            return _tool('success_tool', {'summary': 'plan written'})
        if marker == 'CODEAGENT APPROVE PHASE':
            if self._turn == 1:
                return _tool('ask_user', {'question': 'Approve the plan?'})
            return _tool('success_tool', {'summary': 'approved'})
        if marker == 'CREW CODER - CODING PHASE':
            if self._turn == 1:
                # Round hygiene: a stale stop marker must not survive.
                return _tool('create_file', {
                    'path': str(exchange / STATUS_NAME), 'content': 'IN_PROGRESS'})
            if self._turn == 2:
                return _tool('create_file', {
                    'path': str(exchange / PATCH_NAME),
                    'content': 'diff --git a/x b/x' + chr(10)
                               + '+round ' + str(_entries(marker)) + chr(10)})
            return _tool('set_event_success', {'name': 'code_ready'})
        if marker == 'CREW CODER - SUMMARY PHASE':
            verdict = ''
            review = exchange / REVIEW_NAME
            if review.exists():
                verdict = review.read_text(encoding='utf-8')
            if 'REQUEST_CHANGES' in verdict:
                return _tool('exit_to', {
                    'name': 'Plan',
                    'summary': 'Address the review findings and re-export the '
                               'SAME ' + PATCH_NAME})
            if 'APPROVE' in verdict:
                round_no = len(_SCRIPT['reviewed'])
                if self._turn == 1:
                    return _tool('ask_user', {
                        'question': 'This round is approved. Commit it, or start '
                                    'the next coding task?'})
                if round_no in _SCRIPT['commit_after_round']:
                    return _tool('set_event_success', {'name': 'commit_ready'})
                return _tool('exit_to', {
                    'name': 'Preamble',
                    'summary': 'This round is approved: ask the user for the '
                               'next coding task'})
            return _tool('loop_tool', {'modifier': 'no review found: retry'})
        if marker == 'CREW CODER - COMMIT WAIT PHASE':
            return _tool('exit_to', {
                'name': 'Preamble',
                'summary': 'The lead finished (or declined) the commit gate: '
                           'read the merge record and ask the user for the '
                           'next coding task'})
        if marker == 'CREW CODER - SHUTDOWN PHASE':
            if self._turn == 1:
                return _tool('create_file', {
                    'path': str(exchange / STATUS_NAME), 'content': 'DONE'})
            return _tool('set_event_success', {'name': 'code_ready'})
        if marker == 'CREW REVIEWER - INTAKE PHASE':
            return _tool('success_tool', {'summary': 'folder noted'})
        if marker == 'CREW REVIEWER - REVIEW PHASE':
            status = exchange / STATUS_NAME
            text = (status.read_text(encoding='utf-8').strip()
                    if status.exists() else '')
            if text == 'DONE':
                _SCRIPT['done_relays'] += 1
                return _tool('set_event_success', {'name': 'commit_ready'})
            # Loop-to-self semantics: the reviewer NEVER leaves this stage
            # between rounds (set_event_loop re-parks it on code_ready), so a
            # per-entry `_turn` reset no longer happens. A round is recognised
            # by the PATCH it publishes, not by the stage counter.
            seen = _patch_round(exchange)
            if seen != 'NO PATCH' and seen not in _SCRIPT['reviewed']:
                _SCRIPT['markers_seen'].append(text)
                _SCRIPT['reviewed'].append(seen)
                round_no = len(_SCRIPT['reviewed'])
                verdict = ('APPROVE' if round_no >= _SCRIPT['approve_after']
                           else 'REQUEST_CHANGES')
                body = ('# Review: ' + PATCH_NAME + chr(10)
                        + '## Summary' + chr(10) + 'round ' + str(round_no) + chr(10)
                        + '## Reviewed patch content' + chr(10) + seen + chr(10)
                        + '## Verdict' + chr(10) + verdict + chr(10))
                return _tool('create_file', {
                    'path': str(exchange / REVIEW_NAME),
                    'content': body})
            return _tool('set_event_loop', {'name': 'review_done'})
        return 'done'


class CrewPipelineIntegrationTests(unittest.TestCase):
    def _workspace(self):
        TMP.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix='yaha_crew_', dir=str(TMP)))
        yh = ws / '.yaha'
        yh.mkdir(parents=True)
        (yh / 'memory').mkdir()
        (yh / 'ext').mkdir()
        (yh / 'logs').mkdir()
        (yh / 'tool-schema.md').write_text('[]')
        # The class template is include-based: a workspace needs the whole
        # include closure, not just the root file.
        install_template(yh)
        (yh / 'Model.md').write_text('')
        exchange = ws / 'exchange'
        exchange.mkdir()
        return ws, exchange

    def _run_crew(self, approve_after=3, quit_before_coding=False,
                  seed_marker=None, seed_review=None, commit_after_round=(),
                  merge_answer='approve', quit_after_preamble=None,
                  vcs_denied=False):
        '''Run the crew end to end against a fresh workspace.

        approve_after = which review round approves; quit_before_coding = the
        human quits at the first coder Preamble; seed_marker / seed_review
        pre-populate the exchange folder (stale-artifact scenarios);
        commit_after_round = review rounds after which the coder requests a
        commit (drives the CommitWait/Rearm guard); merge_answer = what the human
        answers at the lead's merge gate; quit_after_preamble = how many coder
        Preamble visits the fake human tolerates before quitting (defaults to
        1, so the FIRST return to Preamble quits - revision rounds jump back
        to Plan and never touch Preamble); vcs_denied = run the
        approved commit action for real so the SANDBOX denies it.
        '''
        ws, exchange = self._workspace()
        if seed_marker is not None:
            (exchange / STATUS_NAME).write_text(seed_marker, encoding='utf-8')
        if seed_review is not None:
            (exchange / REVIEW_NAME).write_text(seed_review, encoding='utf-8')
        _SCRIPT['exchange'] = str(exchange)
        _SCRIPT['approve_after'] = approve_after
        _SCRIPT['quit_before_coding'] = quit_before_coding
        _SCRIPT['visits'] = {}
        _SCRIPT['reviewed'] = []
        _SCRIPT['markers_seen'] = []
        _SCRIPT['order'] = []
        _SCRIPT['gates'] = []
        _SCRIPT['merge_records'] = []
        _SCRIPT['commit_after_round'] = list(commit_after_round)
        _SCRIPT['merge_answer'] = merge_answer
        _SCRIPT['vcs_denied'] = vcs_denied
        _SCRIPT['host_cmd'] = ('patch --dry-run -p1 -d ' + CANONICAL_ROOT
                               + ' < ' + str(exchange / PATCH_NAME))
        _SCRIPT['merge_path'] = str(ws / '.crew' / 'merge' / 'crew_change.merge.md')
        _SCRIPT['denial_seen'] = None
        _SCRIPT['done_relays'] = 0
        if vcs_denied:
            (ws / '.crew' / 'merge').mkdir(parents=True, exist_ok=True)
        _SCRIPT['quit_after_preamble'] = (1
                                          if quit_after_preamble is None
                                          else quit_after_preamble)
        _CrewProvider.exchange = str(exchange)

        def _answer(self, question):
            '''Stand-in for the human at every ask_user prompt.'''
            if 'merge gate' in question:
                return _SCRIPT['merge_answer']
            return 'ok'

        with patch('yaha.llm.ollama.OllamaProvider', _CrewProvider):
            with patch('yaha.core.agent.Agent.ask_user', _answer):
                with patch('sys.stdin', _NonTTY()):
                    eng = YAHAEngine(workspace_dir=str(ws))
                    result = eng.run_agent(
                        'Crewlda',
                        'Implement the change. Shared exchange folder: '
                        + str(exchange))
        return ws, exchange, result

    def test_multi_round_revisions_approve_then_quit_shutdown(self):
        ws, exchange, result = self._run_crew(approve_after=3)
        try:
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith('Error'),
                             'run must not deadlock: ' + result)
            # Three coding rounds: two revisions, then the approved round.
            self.assertEqual(_SCRIPT['visits'].get('CREW CODER - CODING PHASE'), 3)
            # Every review saw ITS OWN round's patch: no handshake leaked.
            self.assertEqual(_SCRIPT['reviewed'],
                             ['round 1', 'round 2', 'round 3'])
            self.assertEqual(_patch_round(exchange), 'round 3')
            review = (exchange / REVIEW_NAME).read_text(encoding='utf-8')
            self.assertIn('APPROVE', review)
            self.assertIn('round 3', review)
            # APPROVE did NOT end the crew: the coder went back to Preamble.
            # (Revision rounds jump back to Plan, so Preamble sees only the
            # initial visit and the post-approval return.)
            self.assertEqual(_SCRIPT['visits'].get('CodeAgent Preamble phase'), 2)
            # The quit released the reviewer, which exited on the DONE marker.
            # (Loop-to-self keeps the reviewer INSIDE its Review stage between
            # rounds, so stage visits no longer count rounds: the three rounds
            # are the `reviewed` list above, and the DONE wake is the relay.)
            self.assertEqual(_SCRIPT['done_relays'], 1)
            status = (exchange / STATUS_NAME).read_text(encoding='utf-8')
            self.assertEqual(status.strip(), 'DONE')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_quit_before_any_coding_releases_the_reviewer(self):
        ws, exchange, result = self._run_crew(quit_before_coding=True)
        try:
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith('Error'),
                             'quit must not deadlock the reviewer: ' + result)
            self.assertFalse((exchange / PATCH_NAME).exists(),
                             'no coding round ran')
            self.assertEqual(
                (exchange / STATUS_NAME).read_text(encoding='utf-8').strip(),
                'DONE')
            # The reviewer was released (parked pulse or latch) and exited.
            self.assertEqual(
                _SCRIPT['visits'].get('CREW REVIEWER - REVIEW PHASE'), 1)
            self.assertEqual(_SCRIPT['reviewed'], [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_stale_stop_marker_cannot_end_the_crew_early(self):
        stale = ('# Review: ' + PATCH_NAME + chr(10) + '## Verdict' + chr(10)
                 + 'REQUEST_CHANGES' + chr(10))
        ws, exchange, result = self._run_crew(approve_after=2,
                                             seed_marker='APPROVED',
                                             seed_review=stale)
        try:
            self.assertFalse(result.startswith('Error'),
                             'stale marker must not deadlock: ' + result)
            # The reviewer reviewed both rounds instead of exiting on the stale
            # APPROVED marker, and the coder had overwritten it each round.
            self.assertEqual(_SCRIPT['reviewed'], ['round 1', 'round 2'])
            self.assertEqual(_SCRIPT['markers_seen'],
                             ['IN_PROGRESS', 'IN_PROGRESS'])
            self.assertEqual(
                (exchange / STATUS_NAME).read_text(encoding='utf-8').strip(),
                'DONE')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_two_commit_requests_in_one_run(self):
        '''The TUI input-lock guard plus the lead's `exit_to` re-entry.

        The human asks to commit after review round 1 AND after round 2. What
        must hold: the coder PARKS in CommitWait while the lead prompts (it takes
        no further turn until `commit_done`), the lead re-parks ITSELF through
        Rearm/`exit_to` instead of falling through the chain, each gate asks the
        user before touching the repository, and the last gate sees `DONE`.
        '''
        ws, exchange, result = self._run_crew(
            approve_after=1, commit_after_round=(1, 2), quit_after_preamble=3)
        try:
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith('Error'),
                             'commit rounds must not deadlock: ' + result)
            order = _SCRIPT['order']
            self.assertEqual(_SCRIPT['reviewed'],
                             ['round 1', 'round 2', 'round 3'])
            # Two mid-run gates plus the final gate.
            self.assertEqual(order.count('CREW LEAD - COMMIT PHASE'), 3)
            # The coder was parked and woken once per commit request.
            self.assertEqual(order.count('CREW CODER - COMMIT WAIT PHASE'), 2)
            # ... and it only resumed AFTER the lead's gate had opened, i.e. it
            # really had released the terminal while the lead prompted.
            self.assertLess(order.index('CREW LEAD - COMMIT PHASE'),
                            order.index('CREW CODER - COMMIT WAIT PHASE'))
            # The lead re-parked itself through exit_to, not by chain fall-through.
            self.assertEqual(order.count('CREW LEAD - REARM PHASE'), 2)
            # Nothing was merged without confirmation; both gates ran, and the
            # final one saw the crew finished.
            self.assertEqual(_SCRIPT['merge_records'], ['approve', 'approve'])
            self.assertEqual([g['status'] for g in _SCRIPT['gates']],
                             ['IN_PROGRESS', 'IN_PROGRESS', 'DONE'])
            self.assertEqual(
                (exchange / STATUS_NAME).read_text(encoding='utf-8').strip(),
                'DONE')
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_declined_commit_still_wakes_the_coder(self):
        '''A declined merge must not strand the parked coder.

        The lead records the declined outcome and STILL signals `commit_done`,
        so the coder leaves CommitWait and carries on; nothing is merged.
        '''
        ws, exchange, result = self._run_crew(
            approve_after=1, commit_after_round=(1,), merge_answer='decline',
            quit_after_preamble=2)
        try:
            self.assertFalse(result.startswith('Error'),
                             'a declined commit must not deadlock: ' + result)
            self.assertEqual(_SCRIPT['merge_records'], ['decline'])
            record = (exchange / 'merge-record.md').read_text(encoding='utf-8')
            self.assertIn('decline', record)
            # The coder was woken by commit_done all the same, ran another
            # round, and finally quit normally.
            self.assertEqual(
                _SCRIPT['order'].count('CREW CODER - COMMIT WAIT PHASE'), 1)
            self.assertEqual(_SCRIPT['reviewed'], ['round 1', 'round 2'])
            self.assertEqual([g['status'] for g in _SCRIPT['gates']],
                             ['IN_PROGRESS', 'DONE'])
            self.assertEqual(
                (exchange / STATUS_NAME).read_text(encoding='utf-8').strip(),
                'DONE')
        finally:
            shutil.rmtree(ws, ignore_errors=True)


    def test_vcs_denied_reports_exact_commands(self):
        '''A denied VCS action must not crash, force, or strand the parked coder.

        The human approves the merge, the lead runs the approved action through
        the REAL `bash_tool`, and the REAL `AccessController` denies it (nothing
        outside the workspace is whitelisted). The lead must record the EXACT
        host command in `<id>.merge.md` and STILL signal `commit_done`.
        '''
        canonical = (Path(CANONICAL_ROOT) / 'yaha' / 'templates'
                     / 'coder.md')
        # Hard prerequisite: the strongest guarantee of this case is that the
        # denied action never touched the canonical tree. A host without that
        # checkout must FAIL loudly instead of silently proving less.
        self.assertTrue(canonical.exists(), CANONICAL_ROOT
                        + ' is required for the nothing-was-forced check')
        before = _digest(canonical)
        ws, exchange, result = self._run_crew(
            approve_after=1, commit_after_round=(1,), vcs_denied=True,
            quit_after_preamble=2)
        try:
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith('Error'),
                             'a denied commit must not deadlock: ' + result)
            # The denial the lead saw came from the sandbox, not from the fake.
            self.assertIsNotNone(_SCRIPT['denial_seen'],
                                 'no denial ever reached the lead')
            self.assertIn('Access Denied', _SCRIPT['denial_seen'])
            # The merge record carries the EXACT host command.
            record = Path(_SCRIPT['merge_path'])
            self.assertTrue(record.exists(), 'no merge record: ' + str(record))
            text = record.read_text(encoding='utf-8')
            self.assertIn('denied', text)
            self.assertIn(_SCRIPT['host_cmd'], text)
            # The coder was parked, and woken only AFTER the gate opened.
            order = _SCRIPT['order']
            self.assertEqual(order.count('CREW CODER - COMMIT WAIT PHASE'), 1)
            self.assertLess(order.index('CREW LEAD - COMMIT PHASE'),
                            order.index('CREW CODER - COMMIT WAIT PHASE'))
            self.assertEqual([g['status'] for g in _SCRIPT['gates']],
                             ['IN_PROGRESS', 'DONE'])
            # Nothing was forced onto the canonical tree.
            self.assertEqual(before, _digest(canonical),
                             'the canonical tree must not change')
            self.assertEqual(
                (exchange / STATUS_NAME).read_text(encoding='utf-8').strip(),
                'DONE')
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
