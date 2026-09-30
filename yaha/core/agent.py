# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import json
import json_repair
import re
import uuid
import os
import sys
import time
import yaml
from .logger import AgentLogger
from ..tools.file_tools import create_file, read_file, edit_file, write_file, search_files, grep_files, append_file, list_files
from ..tools.system_tools import bash_tool, web_fetch_tool, tavily_search_tool
from ..tools.async_tools import async_bash_tool, poll_async_tool
from ..tools.skill_tools import load_skill
from ..ui.terminal import YAHAUI, ASK_INTERRUPT_SENTINEL
from . import interrupt_commands as ic
from .chain import ChainBudget, ChainWalk, StageExit
from .context_calibration import ContextCalibration
from .runtime import RuntimeDeadlockError, is_placeholder
from .async_jobs import AsyncJobRegistry, DONE
from .endpoints import MAX_CONSECUTIVE_ERRORS, EndpointError, is_ep
from collections import namedtuple


# Result of one interrupt-venue session. `status` is the chain-move status the
# venue selected, `arg` is that command's validated argument, and `text` is the
# operator steer carried by /resume (None when the operator resumed bare).
VenueResult = namedtuple('VenueResult', 'status arg text')

# Strict emergency watermark for context compression: at or above this occupancy the
# mid-loop safety net compresses REGARDLESS of the stage's Snap policy.
#
# Deliberately a module constant, and deliberately NOT derived from
# `config.snap_threshold`. The Snap threshold is an opt-in STAGE policy; this is the
# unconditional high-water net. Deriving it from the Snap bar (the historical
# `self.config.snap_threshold or 0.8`) made the net fire at the Snap threshold, which
# thrashed cheap L1 passes at ~80% occupancy and let an L1 pass preempt the Snap that a
# stage had explicitly opted into.
HARD_FORCE_RATIO = 0.95

# Output tokens the strict net must leave free on top of its own prompt. The net has to
# call the model (the Snap handler, or the reflection prompt) over a near-full history, so
# a fixed ratio cannot know whether it still fits: on the 128K models this tree ships
# (max_tokens 8192) five percent is 6400 tokens, i.e. LESS than one maximum request, and
# the handler would be rejected for exceeding the window. `_hard_force_ratio()` therefore
# derives the bar from the model's window and this margin, using HARD_FORCE_RATIO as a CAP.
HARD_FORCE_OUTPUT_MARGIN = 4096

# A window too small to satisfy the invariant sits here rather than somewhere arbitrary;
# no threshold can make the net's own request fit in such a window, and that is documented
# rather than hidden.
HARD_FORCE_FLOOR = 0.5

# Mirrors the provider default for a model that declares no max_tokens
# (`self.config.max_tokens or 4096` in llm/deepseek.py).
HARD_FORCE_DEFAULT_MAX_TOKENS = 4096

# Context-compression retention policy (L2 cold restart AND the Snap handler).
#
# A compressed context is rebuilt as: system prompt + a compact summary + the
# *last n ReAct steps*, so the agent resumes with concrete recent work instead of
# only a digest. `COMPRESSION_KEEP_STEPS` is the DEFAULT n; an agent class overrides
# it with the class-level AgentClass key `Compression Keep Steps`.
COMPRESSION_KEEP_STEPS = 10

# The summary that replaces the earlier conversation is asked to fit in FEWER THAN
# this many lines, and is hard-capped here at runtime as a safety net.
COMPRESSION_SUMMARY_MAX_LINES = 100

# DeepSeek flash models sometimes bypass the harness <tool_code> format and
# emit their native DSML tool-call block instead. Accommodate that dialect for
# exactly these model names (checked together with provider == "deepseek").
_DSML_MODEL_NAMES = frozenset({"deepseek-v4-flash", "deepseek-flash"})

_DSML_TAG = r"[|｜]{2,}DSML[|｜]{2,}"


# --- model-response hardening -------------------------------------------------
# A model must never write the framework observation envelope: only the
# framework reports tool output (see the result message built in run()). These
# detectors are keyed on the framework OWN shape, so ordinary prose that merely
# mentions a result is never rejected.
_TOOL_TAG = 'tool' + '_' + 'code'
_TOOL_BLOCK_START_PATTERNS = (
    '<' + _TOOL_TAG + '>',
    '<' + _DSML_TAG + _TOOL_TAG + '>',
    '<[|｜]{2,}' + _TOOL_TAG + '>',
)
_TOOL_BLOCK_END_PATTERNS = (
    '</' + _TOOL_TAG + '>',
    '</' + _DSML_TAG + _TOOL_TAG + '>',
    '</[|｜]{2,}' + _TOOL_TAG + '>',
    _DSML_TAG + _TOOL_TAG + '>',
    '[|｜]{2,}' + _TOOL_TAG + '>',
    '</' + 'tool' + '_cod' + '[^>]*>',
)

# The framework own envelope fields: both must appear before a body counts as an
# envelope, so a lone Status line in prose is never enough.
_OBSERVATION_HEADER_RE = re.compile(r'^\s*TOOL RESULT\b', re.IGNORECASE | re.MULTILINE)
_OBSERVATION_TAG_RE = re.compile(r'</?\s*tool[_-]?result\b[^>]*>', re.IGNORECASE)
_OBSERVATION_FIELD_RES = (
    re.compile(r'^\s*[-*]\s*\*\*Status\*\*\s*:', re.MULTILINE),
    re.compile(r'^\s*[-*]\s*\*\*Output Size\*\*\s*:', re.MULTILINE),
)
# What the framework itself writes into history, including the compressed form
# that _truncate_old_tool_results produces; either counts as a real observation.
_TOOL_RESULT_PREFIXES = ('TOOL RESULT (', 'COMPRESSED TOOL RESULT (')

_FABRICATED_OBSERVATION_ERROR = (
    'Malformed response: the reply writes the framework observation envelope '
    '(a fabricated tool result). Only the framework reports tool output - make '
    'the call you need instead of inventing its result.'
)
_UNGROUNDED_CLAIM_ERROR = (
    'Malformed response: the reply claims a verified outcome, but this agent has '
    'not run anything yet, so the claim cannot be grounded. Run the command (or '
    'drop the claim) before answering.'
)
# Outcome claims that only a real observation could support. Deliberately narrow:
# it never fires unless the agent also holds no observation at all.
_VERIFIED_CLAIM_RE = re.compile(
    r'\b(?:'
    r'the tests? (?:all )?(?:passed|pass|succeeded)'
    r'|all \d+ tests? pass(?:ed)?'
    r'|the (?:command|suite|gate|build|run) (?:succeeded|passed|failed)'
    r'|i (?:have )?(?:read|opened|checked|inspected) the file'
    r'|the file (?:now )?contains'
    r'|i (?:have )?(?:ran|executed) the (?:command|tests?|suite|script)'
    r'|the gate is green'
    r')',
    re.IGNORECASE,
)


def _strip_reasoning(text):
    '''Return text without its reasoning section (same rules as the parser).'''
    if '<reasoning>' not in text:
        return text
    if '</reasoning>' not in text:
        return text.split('<reasoning>', 1)[0]
    return re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL)


def _strip_tool_blocks(text):
    '''Return text with any tool block removed.

    Quoting the envelope inside a call argument (a plan, a README, a test
    fixture) is legitimate work, so only text OUTSIDE a block is judged.
    '''
    starts = []
    for p in _TOOL_BLOCK_START_PATTERNS:
        starts.extend(re.finditer(p, text, re.IGNORECASE))
    if not starts:
        return text
    first = min(starts, key=lambda m: m.start())
    tail = text[first.end():]
    last_end = None
    for m in re.finditer('|'.join(_TOOL_BLOCK_END_PATTERNS), tail, re.IGNORECASE):
        last_end = m
    if last_end is None:
        return text[:first.start()]
    return text[:first.start()] + tail[last_end.end():]


def _looks_like_fabricated_observation(text):
    '''True when a model wrote the framework observation envelope itself.'''
    if not text:
        return False
    if _OBSERVATION_HEADER_RE.search(text):
        return True
    if _OBSERVATION_TAG_RE.search(text):
        return True
    return all(r.search(text) for r in _OBSERVATION_FIELD_RES)


def _parse_tool_block(text, end_patterns):
    '''Return (call_or_None, last_error_or_None) for the block at text.

    Candidate ends are tried longest-first: that ordering is what lets a nested
    close tag inside a JSON string argument parse correctly.
    '''
    ends = []
    for p in end_patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            ends.append(m.start())
    ends.append(len(text))
    last_err = None
    for end_pos in sorted(set(ends), reverse=True):
        code_block = text[:end_pos].strip()
        if not code_block:
            continue
        try:
            parsed = json_repair.loads(code_block)
        except Exception as e:
            last_err = str(e)
            continue
        if isinstance(parsed, dict) and 'tool' in parsed:
            return parsed, None
    return None, last_err


def _distinct_tool_calls(text, starts, end_patterns):
    '''Return the distinct calls carried by the given start positions.

    Each block is cut at its own first end marker, so two different calls in one
    reply cannot collapse into the same (last) parse.
    '''
    distinct = []
    for start in starts:
        tail = text[start:]
        cut = len(tail)
        for p in end_patterns:
            m = re.search(p, tail, re.IGNORECASE)
            if m and m.start() < cut:
                cut = m.start()
        try:
            parsed = json_repair.loads(tail[:cut].strip())
        except Exception:
            continue
        if isinstance(parsed, dict) and 'tool' in parsed and parsed not in distinct:
            distinct.append(parsed)
    return distinct


def _validate_tool_call_shape(parsed):
    '''Return (call, error) enforcing the documented call shapes.

    A non-string name used to pass the parser and then die on a set-membership
    test in run() (unhashable type), killing the whole run.
    '''
    tool_name = parsed.get('tool')
    if not isinstance(tool_name, str) or not tool_name.strip():
        got = 'nothing' if tool_name is None else type(tool_name).__name__
        return None, ('Malformed tool call: the `tool` key must be a non-empty '
                      'string naming the tool to run, got %s. Re-emit the call with '
                      'a `tool` string and an `args` object.' % got)
    if 'args' in parsed:
        stray = sorted(k for k in parsed if k not in ('tool', 'args'))
        if stray:
            return None, ('Malformed tool call: unexpected key(s) %s beside `args`. '
                          'Use exactly one `tool` key plus an `args` object, or the '
                          'inlined shape with the arguments as siblings of `tool`.'
                          % ', '.join(repr(k) for k in stray))
    return parsed, None
def _parse_dsml_invoke(text):
    """Parse the FIRST DeepSeek DSML tool invoke into {"tool", "args"}.

    Expected dialect (one tool per turn is all the harness consumes):

        <｜｜DSML｜｜ calls>
        <｜｜DSML｜｜ invoke name="bash_tool">
        <｜｜DSML｜｜ parameter name="args" string="false">{"cmdline": "..."}</｜｜DSML｜｜ parameter>
        </｜｜DSML｜｜ invoke>
        </｜｜DSML｜｜ calls>

    An `args` parameter holds the whole argument JSON; otherwise the named
    parameters are collected into a plain string dict. Returns None when no
    usable invoke is present so the caller can keep its normal error path.
    """
    if not text:
        return None
    invoke = re.search(
        r"<" + _DSML_TAG + r"\s*invoke\s+name=\"([^\"]+)\"[^>]*>",
        text, re.IGNORECASE,
    )
    if invoke is None:
        return None
    # Scope parameter extraction to this invoke's body.
    close = re.search(r"</" + _DSML_TAG + r"\s*invoke\s*>", text[invoke.end():], re.IGNORECASE)
    body = text[invoke.end():invoke.end() + close.start()] if close else text[invoke.end():]
    params = re.findall(
        r"<" + _DSML_TAG + r"\s*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</" + _DSML_TAG + r"\s*parameter\s*>",
        body, re.IGNORECASE | re.DOTALL,
    )
    args = {}
    args_blob = next((v for k, v in params if k.strip().lower() == "args"), None)
    if args_blob is not None:
        try:
            parsed = json_repair.loads(args_blob.strip())
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        args = parsed
    else:
        args = {k.strip(): v.strip() for k, v in params}
    return {"tool": invoke.group(1), "args": args}


class _PromptGuardUI:
    """Delegate a UI, suspending the Esc controller around prompt_user.

    The AccessController security question and the API retry wait/break prompt
    call ``ui.prompt_user`` directly (not via Agent._prompt_interrupt_safe).
    While the engine's stdin controller is active it holds stdin in cbreak mode
    and its reader discards non-ESC bytes, which steals the operator's first
    keystroke. This proxy suspends the controller (and restores cooked mode)
    for the duration of the prompt, then resumes it. Everything else delegates
    to the wrapped UI.
    """

    def __init__(self, ui, suspend, resume):
        self._ui = ui
        self._suspend = suspend
        self._resume = resume

    def prompt_user(self, *args, **kwargs):
        self._suspend()
        try:
            return self._ui.prompt_user(*args, **kwargs)
        finally:
            self._resume()

    def __getattr__(self, name):
        return getattr(self._ui, name)


def _tool_result_failed(text):
    """Best-effort failure test for a tool result string.

    Tools report failure as an ``Error``/``ERROR`` prefixed string - that is the
    only success/failure channel the tool layer has. The stage-hook observation
    (`yaha.tool.ok`) uses this stripped, case-insensitive form so a tool that
    reports ``ERROR: ...`` is not silently described as successful to a
    `posttool` hook.

    It is deliberately NOT the predicate the ReAct loop uses for stage control
    (`str(tool_result).startswith("Error")` below): broadening that one would
    change stage-completion behaviour for existing tools, which is outside a
    hardening change. A custom tool whose failure text does not start with
    "error" is still reported as `ok=True` - documented in yaha/docs/hooks.md.
    """
    return str(text).strip().lower().startswith("error")


class Agent:
    # Occupancy-instrument defaults. Declared on the CLASS, not only in __init__, so a
    # partially constructed instance (tests build agents with object.__new__) still has
    # defined compression state instead of exploding on the first usage report.
    _last_prompt_tokens = 0
    _chars_at_last_usage = None
    _usage_is_predicted = False
    _stage_peak_ratio = 0.0

    # Marker prefix for a tool result reporting an UNBOUND event placeholder.
    # The control-tool dispatch turns exactly this result into a STAGE FAILURE
    # (routed through the stage's Fail Target); every OTHER "Error: ..." from an
    # event terminator merely keeps the stage alive for a retry.
    _UNRESOLVED_PLACEHOLDER = "Error: unresolved event placeholder"

    def __init__(self, agent_config, session, caller_id=None, models_config=None, agent_classes=None, tool_schemas=None, soul_prompt="", verbose=False, access_controller=None, unattended=None):
        self.config = agent_config
        self.session = session
        self.agent_id = str(uuid.uuid4())[:8]
        self.caller_id = caller_id
        self.models_config = models_config or {}
        self.agent_classes = agent_classes or {}
        self.tool_schemas = tool_schemas or []
        self.soul_prompt = soul_prompt
        self.verbose = verbose
        self.access_controller = access_controller
        self.logger = AgentLogger(session, agent_config.name, self.agent_id, caller_id)
        self.ui = YAHAUI(verbose=verbose)
        self.history = []
        self.loop_limit = getattr(self.config, "max_loops", -1)
        self.restart_count = 0
        self.last_final_answer = None
        # The walk index, the exit_to call frame and the Max-Loops accounting
        # live in `core/chain.py` (ChainWalk / ChainBudget), not on the agent.

        # Context auto-compression state
        self.context_size = 128000
        self.compress_count = 0
        self.current_task_prompt = ""
        # One-shot modifier set by loop_tool / set_event_loop; appended to the
        # re-entered stage's prompt as a [TASK MODIFIER] section, then cleared.
        self._loop_modifier = ""
        # One-shot remote notice set by an endpoint wait ([REMOTE MESSAGE] /
        # [REMOTE CONNECTION]); appended to the stage prompt, then cleared.
        self._remote_notice = ""
        # SNAP MARKER: the last context snapshot, recorded so a snap is a VISIBLE,
        # attributable event rather than a silent history swap. `signalled` lists
        # the multi-agent events the current stage had already emitted when the
        # snap fired.
        self._snap_marker = None
        # Events the CURRENT stage has signalled; cleared at each stage entry so
        # the marker reports only this stage's signals.
        self._stage_signalled_events = set()
        self._last_prompt_tokens = 0
        # Character count of the history at the moment `_last_prompt_tokens` was
        # reported. None means "no baseline recorded", in which case the anchor is
        # used as-is (no delta). This keeps the occupancy instrument grounded on a
        # provider measurement instead of a bare characters/4 reading.
        self._chars_at_last_usage = None
        # True while the occupancy shown to the user is a prediction rather than a
        # fresh provider count (set by every compression, cleared by every usage
        # report). The UI must be able to say so.
        self._usage_is_predicted = False
        # Highest corrected occupancy seen during the current stage. The Snap gate
        # reads it so a cheap L1 pass cannot cancel the Snap a stage opted into.
        self._stage_peak_ratio = 0.0
        self._calibration = ContextCalibration()
        self._compressing = False
        # The prompt of the stage currently running, so a venue /compress can
        # re-inject it after forcing a mid-stage compression.
        self._current_stage_prompt = ''
        self._last_snap_rebuilt = False
        # User-interrupt (Esc) state. The engine owns the shared stdin/Esc
        # controller (installed for interactive root runs, dormant otherwise);
        # this agent only suspends it around prompts and polls it for Esc.
        self.runtime = None
        self.stdin_interrupt = None
        self._serving_interrupt = False
        # Stage hooks (preenter/pretool/posttool/preexit). Lazily created on the
        # first hook of a stage; a class that declares no hooks never allocates
        # an engine, so hook-free runs are unchanged.
        self._hook_engine_obj = None
        # One-shot chain move requested by an interrupt served from ask_user.
        # Cleared by the ReAct loop when it is consumed.
        self._pending_interrupt_status = None
        # Diagnose-venue context (read by /status and the stage-boundary poll).
        # All additive: nothing here changes existing behaviour on its own.
        self._current_turn = 0
        self._last_stage_turn = 0
        self._last_stage_tools_called = set()
        self._chain_index = 0
        self._chain_len = 0
        self._venue_entry = None
        self._last_tool_result = None
        # API retry tunables (per exhaustion round / user-wait behavior).
        self.llm_retry_limit = 10
        self.api_retry_prompt_timeout = 600
        self.api_retry_wait_seconds = 600
        # Unattended (no TTY / no user reply) rounds allowed before graceful abort.
        self.api_retry_unattended_max_rounds = 3
        # Explicit unattended mode. None means infer from stdin TTY later.
        self.unattended = unattended
        
        # Background async jobs (`async_bash`): inside a multi-agency run the
        # run-global `runtime.async_jobs` is used (see `_async_registry`);
        # this local registry serves a single-agent run instead.
        self._async_jobs_local = None

        # Initialize Tool Map with Access Control
        self.tool_map = {
            "create_file": lambda path, content: create_file(path, content, access_controller=self.access_controller, ui=self.ui),
            # The two unbounded-output tools get a per-call byte budget: half of
            # the context window still free (see tools/output_guard.py). It is
            # resolved when the model calls the tool, never at construction.
            "read_file": lambda path, start_line, end_line: read_file(path, start_line, end_line, access_controller=self.access_controller, ui=self.ui, max_output_bytes=self._tool_output_budget()),
            "edit_file": lambda path, **kwargs: edit_file(path, access_controller=self.access_controller, ui=self.ui, **kwargs),
            "write_file": lambda path, content, start_line: write_file(path, content, start_line, access_controller=self.access_controller, ui=self.ui),
            "search_files": lambda path, search_text: search_files(path, search_text, access_controller=self.access_controller, ui=self.ui),
            "grep_files": lambda path, search_text, recursive=True, glob_pattern=None, max_results=100: grep_files(path, search_text, recursive=recursive, glob_pattern=glob_pattern, max_results=max_results, access_controller=self.access_controller, ui=self.ui),
            "append_file": lambda path, content: append_file(path, content, access_controller=self.access_controller, ui=self.ui),
            "list_files": lambda path=".", pattern=None: list_files(path, pattern, access_controller=self.access_controller, ui=self.ui),
            "bash_tool": self._bash_tool_with_guard,
            "async_bash": self._async_bash_tool_with_guard,
            "poll_async": lambda job_id: poll_async_tool(
                job_id, registry=self._async_registry()),
            "web_fetch_tool": web_fetch_tool,
            "load_skill": lambda name: load_skill(name, session=self.session),
            "yaha_spawn_agent": lambda **kwargs: self.yaha_spawn_agent(**kwargs),
            "google_search_tool": lambda **kwargs: self.google_search_tool(**kwargs),
            "tavily_search_tool": lambda query, max_results=5: tavily_search_tool(query, max_results=max_results),
            "ask_user": self.ask_user,
            "restart": self.restart_impl,
            "loop_tool": self.loop_tool_impl,
            "success_tool": lambda summary="": summary,
            "fail_tool": lambda reason="": reason,
            "set_event_success": self.set_event_success,
            "set_event_loop": self.set_event_loop,
            "set_event_jump": self.set_event_jump,
            "bind_tool": self.bind_tool,
            "connect_tool": self.connect_tool,
            "send_tool": self.send_tool,
            "close_tool": self.close_tool,
            "exit_to": self.exit_to_impl,
            "spawn_parallel_agent": lambda **kwargs: self.spawn_parallel_agent(**kwargs),
        }
        
        # Initialize LLM Provider
        model_name = self.config.model
        model_cfg = self.models_config.get(model_name)
        if not model_cfg:
            # Fallback if model not found in config
             from ..config.models import ModelConfig
             model_cfg = ModelConfig(name=model_name, provider="ollama", model=model_name)
        self.model_cfg = model_cfg
        # Strict accommodation: only deepseek flash models may speak native
        # DSML tool calls; every other model keeps the hard malformed error.
        self._dsml_accommodate = (
            model_cfg.provider == "deepseek"
            and (model_cfg.model or "").lower() in _DSML_MODEL_NAMES
        )
        
        if model_cfg.provider == "gemini":
            from ..llm.gemini import GeminiProvider
            self.llm = GeminiProvider(model_cfg)
        elif model_cfg.provider == "deepseek":
            from ..llm.deepseek import DeepSeekProvider
            self.llm = DeepSeekProvider(model_cfg)
        else:
            from ..llm.ollama import OllamaProvider
            self.llm = OllamaProvider(model_cfg)

        self.context_size = model_cfg.context_size or 128000
        # Show live context-window usage on every TUI section bar.
        self.ui.set_ctx_provider(self._ctx_display_text)

    def build_system_prompt(self):
        prompt = ""
        if self.soul_prompt:
            prompt += self.soul_prompt + "\n\n"
        prompt += self.config.system_prompt + "\n\n"
        
        # Inject Runtime Context
        from datetime import datetime
        prompt += f"RUNTIME CONTEXT:\n- Today's date: {datetime.now().strftime('%Y-%m-%d')}\n"
        prompt += f"- Current Working Directory (CWD): {os.getcwd()}\n"
        prompt += f"- Memory folder: {os.path.join('.yaha', 'memory')}\n"
        prompt += f"- Session Folder: {self.session.folder_name}\n\n"

        # Automatic Skill Discovery
        if self.config.skillset_mode == "Auto":
            skill_roots = self.session.get_skill_roots()
            
            available_skills = {}
            
            for skills_dir in skill_roots:
                if os.path.exists(skills_dir):
                    # Sort to ensure stable order within a directory
                    for skill_name in sorted(os.listdir(skills_dir)):
                        if skill_name in available_skills:
                            continue
                            
                        skill_path = os.path.join(skills_dir, skill_name, "SKILL.md")
                        if os.path.exists(skill_path):
                            try:
                                with open(skill_path, "r", encoding="utf-8") as f:
                                    content = f.read()
                                    if content.startswith("---"):
                                        parts = content.split("---", 2)
                                        if len(parts) >= 3:
                                            frontmatter = yaml.safe_load(parts[1])
                                            loc = os.path.join(skills_dir, skill_name)
                                            if loc.startswith(os.getcwd()):
                                                loc = os.path.relpath(loc, os.getcwd())
                                                
                                            available_skills[skill_name] = {
                                                "name": skill_name,
                                                "description": frontmatter.get("description", "No description available."),
                                                "location": loc
                                            }
                            except Exception as e:
                                # Skip malformed skills
                                pass
                
            if available_skills:
                    prompt += "<available_skills>\n"
                    prompt += "NOTE: The <location> tag for each skill contains the path to its root directory relative to the CWD. When a skill's instructions refer to a script or resource using a relative path, you MUST prefix it with this location to execute it correctly.\n"
                    for s in available_skills.values():
                        prompt += f"  <skill>\n"
                        prompt += f"    <name>{s['name']}</name>\n"
                        prompt += f"    <description>{s['description']}</description>\n"
                        prompt += f"    <location>{s['location']}</location>\n"
                        prompt += f"  </skill>\n"
                    prompt += "</available_skills>\n\n"

        # Inject Tool Schemas
        available_tools = [s for s in self.tool_schemas if s["name"] in self.config.toolset]
        # Always add internal tools if they are not in the schema
        if "restart" not in [t["name"] for t in available_tools]:
             available_tools.append({
                 "name": "restart",
                 "description": "Restarts the prompt chain from the beginning (Preamble + Task). Use this if you realize the current trajectory is fundamentally flawed or if the environment state has changed in a way that requires a full reset.",
                 "parameters": {"type": "object", "properties": {}, "required": []}
             })
        if "loop_tool" not in [t["name"] for t in available_tools]:
             available_tools.append({
                 "name": "loop_tool",
                 "description": "Loops the CURRENT stage: the stage ends and is immediately re-entered (its Wait Event gate is re-checked, its prompt rebuilt). Preserves conversation history. To move to a DIFFERENT stage use exit_to instead.",
                 "parameters": {
                     "type": "object",
                     "properties": {
                         "summary": {
                             "type": "string",
                             "description": "Optional concise summary of this stage; becomes the stage's final response."
                         },
                         "modifier": {
                             "type": "string",
                             "description": "Optional one-shot note appended to the re-entered stage's prompt as a [TASK MODIFIER] section."
                         }
                     },
                     "required": []
                 }
             })

        # Inject stage-exit tools if any stage declares exit_tools
        exit_tools_used = self._exit_tools_used()
        if "success_tool" in exit_tools_used and "success_tool" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "success_tool",
                "description": "Signals successful completion of the current stage and advances to the next stage. Only use this when the stage instructions explicitly require an exit tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Optional concise summary of what was accomplished in this stage."
                        }
                    },
                    "required": []
                }
            })
        if "fail_tool" in exit_tools_used and "fail_tool" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "fail_tool",
                "description": "Signals that the current stage has failed and routes to the designated failure-handling stage. Use this when the stage cannot complete successfully.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Optional reason for the failure."
                        }
                    },
                    "required": []
                }
            })

        if "set_event_success" in exit_tools_used and "set_event_success" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "set_event_success",
                "description": "Set one or more named multi-agent events, then complete the current stage (set_event + success_tool). Peers waiting on any of those events at a stage boundary are woken. Only use this when the stage instructions explicitly require it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
                        },
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The event name(s) to set before leaving the stage. At least one is required."
                        }
                    },
                    "required": ["names"]
                }
            })
        if "set_event_jump" in exit_tools_used and "set_event_jump" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "set_event_jump",
                "description": "Set one or more named multi-agent events, then move the chain to the target stage (set_event + exit_to). Peers waiting on any of those events are woken. The target obeys the exit_to rules: a chain stage is a positional jump, a declared Detached stage is a CALL, and naming the CURRENT stage is refused (use set_event_loop to re-enter it).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
                        },
                        "target": {
                            "type": "string",
                            "description": "The stage to move to: a chain stage (jump) or a declared Detached stage (call)."
                        },
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The event name(s) to set before leaving the stage. At least one is required."
                        }
                    },
                    "required": ["target", "names"]
                }
            })
        if "set_event_loop" in exit_tools_used and "set_event_loop" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "set_event_loop",
                "description": "Set one or more named multi-agent events, then loop the CURRENT stage (set_event + loop_tool): the stage ends and is immediately re-entered. Peers waiting on any of those events are woken.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
                        },
                        "modifier": {
                            "type": "string",
                            "description": "Optional one-shot note appended to the re-entered stage's prompt as a [TASK MODIFIER] section."
                        },
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The event name(s) to set before looping the stage. At least one is required."
                        }
                    },
                    "required": ["names"]
                }
            })

        if "exit_to" in exit_tools_used and "exit_to" not in [t["name"] for t in available_tools]:
            available_tools.append({
                "name": "exit_to",
                "description": "Leave the current stage and continue the chain from the named stage. A chain-stage name is a positional jump (forward or backward; naming the CURRENT stage is refused - use loop_tool to re-enter it). A name that is a declared but UNATTACHED (Detached) stage is a CALL: it runs as a full stage in this agent, then the chain resumes at the caller's next stage (calls may nest). A blank or unknown name is a hard error and the stage is NOT left. Costs one chain operation against Max Loops.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The stage to move to: a chain stage (jump) or a declared Detached stage (call)."
                        },
                        "summary": {
                            "type": "string",
                            "description": "Optional concise summary of what this stage accomplished. It becomes the stage's final response."
                        }
                    },
                    "required": ["name"]
                }
            })


        prompt += "AVAILABLE TOOLS:\n"
        prompt += json.dumps(available_tools, indent=2) + "\n\n"

        # Inject Authorized Sub-Agents
        prompt += "AUTHORIZED SUB-AGENTS:\n"
        prompt += "You can ONLY spawn sub-agents of the following classes:\n"
        for agent_name in self.config.agentset:
            desc = ""
            if agent_name in self.agent_classes:
                desc = self.agent_classes[agent_name].description
            
            prompt += f"- {agent_name}\n"
            if desc:
                prompt += f"  - Description: {desc}\n"
                
        prompt += "- Inherited (spawns a sub-agent of your own class)\n\n"

        # Inject Memory Index
        memory_files = self._memory_index_entries()
        if memory_files:
            prompt += "MEMORY INDEX:\n"
            prompt += "The following files are available in your memory folder. You can read them using `read_file` if you need more context on specific topics.\n"
            prompt += "\n".join(memory_files) + "\n\n"

        return prompt

    def _memory_index_entries(self):
        """The name/description lines of the memory folder index.

        Shared by build_system_prompt (the MEMORY INDEX section) and the venue
        /memory command, so the operator sees exactly what the agent was given.
        """
        memory_dir = os.path.join(self.session.workspace_dir, ".yaha", "memory")
        entries = []
        if not os.path.exists(memory_dir):
            return entries
        for f in sorted(os.listdir(memory_dir)):
            if not f.endswith(".md"):
                continue
            file_path = os.path.join(memory_dir, f)
            desc = "No description available."
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            frontmatter = yaml.safe_load(parts[1])
                            if isinstance(frontmatter, dict):
                                desc = frontmatter.get("description", desc)
            except Exception:
                pass
            entries.append(f"- {f}\n  - Description: {desc}")
        return entries

    def yaha_spawn_agent(self, system, instruction, agent_class=None):
        # Implementation of RAC
        return "ERROR: yaha_spawn_agent not yet fully initialized."

    def spawn_parallel_agent(self, agent_class, clone_context="new", prompt=None):
        # Wired by the engine whenever a multi-agency runtime is active.
        return "ERROR: spawn_parallel_agent not yet initialized."

    @staticmethod
    def _normalise_event_names(names):
        """Drop empty and duplicate event names, keeping the caller's order."""
        out = []
        for name in names or []:
            text = str(name or '').strip()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _event_names_from_kwargs(kwargs):
        """Pull event names out of a tool call's keyword arguments.

        The canonical JSON form carries `names` (a list). The tolerated forms
        are numbered keys (name1, name2, ...) and a bare `name`. They may be
        mixed in one call, because the MODEL writes these arguments, not a
        schema validator.
        """
        collected = []
        raw = (kwargs or {}).get('names')
        if isinstance(raw, (list, tuple, set)):
            collected.extend(raw)
        elif raw is not None:
            collected.append(raw)
        numbered = []
        for key, value in (kwargs or {}).items():
            text = str(key)
            if text == 'name':
                collected.append(value)
            elif text.startswith('name') and text[4:].isdigit():
                numbered.append((int(text[4:]), value))
        for _index, value in sorted(numbered, key=lambda pair: pair[0]):
            collected.append(value)
        return collected

    def _validate_event_names(self, tool_name, names):
        """Return an error string when NO event name was given, else None.

        Split out of the event terminators so a failing `preexit (required)`
        hook can be evaluated BEFORE any event is actually signalled: validating
        without signalling is what lets a hook block the handshake. At least one
        name is a hard error because a silent success would leave every waiter
        on those events deadlocked.
        """
        if self.runtime is not None and not names:
            return ("Error: %s requires a non-empty event name (pass names, "
                    "name1, name2, ... or name)." % tool_name)
        return None

    def bind_tool(self, placeholder, real_event):
        """Late-bind a placeholder (`@name`) to a REAL event name.

        A placeholder lets a stage declare a wait - or signal an event - before
        the concrete event name is known. The binding is RUN-GLOBAL and
        strictly 1:1, and REBINDING IS ALLOWED (the LAST bind wins). Shape
        problems are reported as an ordinary `Error: ...` tool result, so a
        malformed call never kills the stage; an UNBOUND placeholder, by
        contrast, fails the stage that uses it.
        """
        if self.runtime is None:
            return "Error: bind_tool requires an active multi-agency runtime."
        key = str(placeholder or "").strip()
        value = str(real_event or "").strip()
        if not is_placeholder(key):
            return ("Error: bind_tool's first argument must be a placeholder "
                    "starting with '@' (got %r)." % key)
        if not value:
            return ("Error: bind_tool requires a non-empty real event name as "
                    "its second argument.")
        if is_placeholder(value):
            return ("Error: bind_tool's second argument must be a REAL event "
                    "name, not a placeholder (%r); chaining placeholders is "
                    "not supported." % value)
        self.runtime.bind_event(key, value)
        return "BOUND_EVENT: %s -> %s" % (key, value)

    def _resolve_event_name(self, name):
        """Resolve an event name; return `(real_name, error_or_None)`.

        A plain name resolves to itself. A placeholder resolves through the
        run-global binding registry; when it is UNBOUND the returned error is
        the stage-failure message and the real name is None, because an
        unresolved placeholder can never be satisfied.
        """
        text = str(name or "").strip()
        if not is_placeholder(text):
            return text, None
        real = None
        if self.runtime is not None:
            real = self.runtime.resolve_event(text)
        if not real:
            return None, (self._UNRESOLVED_PLACEHOLDER
                          + (" %r: bind it first with bind_tool(%r, "
                             "'<real-event>')." % (text, text)))
        return real, None

    def _resolve_event_names(self, names):
        """Resolve a list of event names; return `(resolved, error_or_None)`.

        Used by the event terminators so that an unbound placeholder is refused
        BEFORE any event is signalled - a stage must never half-signal a
        handshake it cannot complete.
        """
        resolved = []
        for name in names or []:
            real, error = self._resolve_event_name(name)
            if error:
                return None, error
            resolved.append(real)
        return resolved, None

    # --- remote endpoints -------------------------------------------------

    def _endpoint_registry(self):
        """The run's endpoint registry, or None outside a multi-agency run."""
        if self.runtime is None:
            return None
        return getattr(self.runtime, "endpoints", None)

    def _resolve_cep_argument(self, cep):
        """Resolve a cep argument to a CEP; return `(cep, error_or_None)`.

        A placeholder is resolved through the binding registry first, so a stage
        can reply on the channel it bound without copying a URL.
        """
        registry = self._endpoint_registry()
        if registry is None:
            return None, ("Error: remote endpoints require an active "
                          "multi-agency runtime.")
        text = str(cep or "").strip()
        if not text:
            return None, "Error: a conversation endpoint is required."
        if is_placeholder(text):
            bound = self.runtime.resolve_event(text)
            if not bound:
                return None, (self._UNRESOLVED_PLACEHOLDER
                              + (" %r: bind it first with bind_tool(%r, "
                                 "'<cep-url>')." % (text, text)))
            text = bound
        found = registry.ceps.get(text)
        if found is None:
            return None, ("Error: %r is not a live conversation endpoint of "
                          "this run." % text)
        return found, None

    def connect_tool(self, service_ep):
        """Open a conversation with a remote service EP; returns its CEP.

        The CEP behaves like any other bound channel: bind it with `bind_tool`
        and wait on the placeholder, and answer with `send_tool`.
        """
        registry = self._endpoint_registry()
        if registry is None:
            return "Error: connect_tool requires an active multi-agency runtime."
        try:
            cep = registry.connect(str(service_ep or "").strip())
        except EndpointError as exc:
            return "Error: %s" % exc
        except Exception as exc:
            return "Error: could not reach %s: %s" % (service_ep, exc)
        return "CONNECTED %s" % cep.url

    def send_tool(self, cep, message):
        """Send one message on a conversation; a placeholder cep is bound."""
        registry = self._endpoint_registry()
        if registry is None:
            return "Error: send_tool requires an active multi-agency runtime."
        target, error = self._resolve_cep_argument(cep)
        if error:
            return error
        try:
            registry.send(target, message)
        except EndpointError as exc:
            return "Error: %s" % exc
        except Exception as exc:
            return "Error: could not send on %s: %s" % (cep, exc)
        return "SENT"

    def close_tool(self, cep):
        """Close a conversation (courtesy: the run reclaims everything anyway)."""
        registry = self._endpoint_registry()
        if registry is None:
            return "Error: close_tool requires an active multi-agency runtime."
        target, error = self._resolve_cep_argument(cep)
        if error:
            return error
        registry.close(target)
        return "CLOSED"

    def _await_endpoint(self, name):
        """Resolve ONE endpoint wait, yielding the park token when it blocks.

        Returns ``(resolved_name, error_or_None, notice_or_None)``. A known CEP
        is a LEVEL read over its inbox queue: take one message and clear
        NOTHING. Anything else is treated as a SERVICE endpoint, whose listener
        starts on first use and whose pending-connection queue delivers the CEP
        url of the new conversation.
        """
        registry = self._endpoint_registry()
        if registry is None:
            return name, ("Error: a remote endpoint wait requires an active "
                          "multi-agency runtime."), None
        cep = registry.ceps.get(name)
        if cep is not None:
            while True:
                message = cep.take()
                if message is not None:
                    return name, None, "[REMOTE MESSAGE]: " + str(message)
                if cep.closed:
                    return name, ("Error: conversation %s is closed."
                                  % name), None
                channel = registry.channels.get(name)
                if channel is not None and getattr(channel, "degraded", False):
                    return name, ("Error: remote channel %s is degraded."
                                  % name), None
                # A wake that produced no message (another waiter took it, or a
                # spurious wake) must RE-PARK: failing the stage here would break
                # a legitimate peer waiting on the same conversation.
                yield ("wait", name)
        try:
            service = registry.service_ep(name)
        except EndpointError as exc:
            return name, "Error: %s" % exc, None
        url = service.url
        while True:
            pending = service.pending.take()
            if pending is not None:
                return url, None, "[REMOTE CONNECTION]: " + str(pending)
            if not service.started:
                return url, ("Error: service endpoint %s is not listening."
                             % url), None
            # Same rule as the CEP branch: one inbound connect wakes EVERY
            # waiter on this service EP, so the losers must re-park rather than
            # fail a stage that did nothing wrong.
            yield ("wait", url)

    def _set_event(self, name):
        """Signal a runtime event (no-op outside a multi-agency run)."""
        if self.runtime is not None and name:
            self.runtime.set_event(name)

    def _set_events(self, names):
        """Signal every named event, recording it for the SNAP MARKER."""
        for name in names or []:
            if name:
                try:
                    self._stage_signalled_events.add(str(name))
                except Exception:
                    pass
            self._set_event(name)

    def set_event_success(self, summary="", *event_names, **kwargs):
        """Set the named events, then complete the stage (like success_tool).

        `summary` is the RESULT OF THIS STAGE, exactly as it was before the
        multi-event tool existed: it becomes the stage's final response. It is
        NOT a prompt prefix - nothing is injected into any later stage.

        At least one event name is a hard error: signalling is the whole point,
        and a silent success would leave every waiter deadlocked.
        """
        names = self._normalise_event_names(
            list(event_names) + self._event_names_from_kwargs(kwargs))
        error = self._validate_event_names("set_event_success", names)
        if error:
            return error
        names, error = self._resolve_event_names(names)
        if error:
            return error
        self._set_events(names)
        return "SET_EVENT_SUCCESS_SIGNALED"

    def set_event_loop(self, summary="", modifier="", *event_names, **kwargs):
        """Set the named events, then loop the CURRENT stage (like loop_tool).

        `summary` is the RESULT OF THIS STAGE, matching `set_event_success`;
        nothing is injected into any later stage. `modifier` is a one-shot
        note appended to the re-entered stage's prompt as a [TASK MODIFIER]
        section, exactly as in loop_tool.

        At least one event name is a hard error: signalling is the whole
        point, and a silent loop would leave every waiter deadlocked.
        """
        names = self._normalise_event_names(
            list(event_names) + self._event_names_from_kwargs(kwargs))
        error = self._validate_event_names("set_event_loop", names)
        if error:
            return error
        names, error = self._resolve_event_names(names)
        if error:
            return error
        self._set_events(names)
        self._loop_modifier = str(modifier or "").strip()
        return "LOOP_SIGNALED"

    def set_event_jump(self, summary="", target="", *event_names, **kwargs):
        """Set the named events, then move the chain to `target`.

        `target` obeys the exit_to rules: a chain stage is a positional jump,
        a declared Detached stage is a CALL, and naming the CURRENT stage is
        refused (use set_event_loop to re-enter it). `summary` is the RESULT
        OF THIS STAGE, exactly as in set_event_success.

        At least one event name is a hard error: signalling is the whole
        point, and a silent jump would leave every waiter deadlocked.
        """
        names = self._normalise_event_names(
            list(event_names) + self._event_names_from_kwargs(kwargs))
        error = self._validate_event_names("set_event_jump", names)
        if error:
            return error
        if not str(target or "").strip():
            return ("Error: set_event_jump requires a non-empty 'target' "
                    "(the stage to move to).")
        names, error = self._resolve_event_names(names)
        if error:
            return error
        self._set_events(names)
        return "SET_EVENT_JUMP_SIGNALED"

    # --- stage hooks -------------------------------------------------------

    @staticmethod
    def _stage_has_hooks(stage):
        hooks = (stage or {}).get("hooks") or {}
        return any(bool(hooks.get(name)) for name in hooks)

    def _hooks(self, stage=None):
        """The agent's HookEngine, or None when the stage declares no hooks.

        Returning None for a hook-free stage is what keeps this feature
        completely inert for classes that do not use it.
        """
        if stage is not None and not self._stage_has_hooks(stage):
            return None
        if self._hook_engine_obj is None:
            from ..hooks.runner import HookEngine
            self._hook_engine_obj = HookEngine(self)
        return self._hook_engine_obj

    def _shutdown_hooks(self):
        """Terminate this agent's python kernel, if it ever started one."""
        engine = self._hook_engine_obj
        self._hook_engine_obj = None
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass
        # A LOCAL async-job registry (single-agent run) can hold live Popen
        # children; kill them so a long-lived process (the ChatAgent loop)
        # cannot leak a child when a nested run is aborted.
        local = self._async_jobs_local
        if local is not None:
            try:
                local.shutdown()
            except Exception:
                pass

    def _hook_observation(self, label, text, turn):
        """Inject a (capture) hook's output into the conversation.

        The observation is attributed to the hook and appended as a user-side
        message, never as a fake assistant turn.
        """
        from ..hooks.runner import truncate
        text = truncate(text)
        if not text.strip():
            return
        self.history.append({
            "role": "user",
            "content": "[HOOK %s]\n%s" % (label, text),
        })
        try:
            self.logger.log_turn(turn, self.history)
        except Exception:
            pass

    def _apply_observations(self, outcome, turn):
        for label, text in outcome.observations:
            self._hook_observation(label, text, turn)

    @staticmethod
    def _hook_failure_message(outcome):
        """The one wording for a failed required hook.

        Shared by the `preenter` routing inside `iter_run` and by
        `_hook_stage_failure`, so the two paths cannot drift apart.
        """
        label = outcome.blocked_label or "hook"
        return ("Error: required hook %s failed: %s"
                % (label, " ".join(str(outcome.detail or "").split())))

    @staticmethod
    def _stage_failure_target(stage):
        """The stage's failure target: its `Fail Target`, default `aftermath`.

        ONE definition, used by the tool path, the `preenter` routing and the
        `/quit` routing, so a missing `Fail Target` cannot default differently
        depending on which path raised the failure.
        """
        return str((stage or {}).get("fail_target") or "aftermath")

    @staticmethod
    def _chain_target_index(chain, target, fallback):
        """Index of the stage named ``target`` in ``chain``, else ``fallback``.

        Case-insensitive, and the ONE lookup shared by the `preenter` routing
        and the `FAIL:` routing, so the same target name cannot resolve to
        different stages depending on which code path raised the failure.
        """
        wanted = str(target or "").lower()
        for index, stage in enumerate(chain or []):
            if str((stage or {}).get("name", "")).lower() == wanted:
                return index
        return fallback

    @staticmethod
    def _fail_termination_answer(outputs, stage_name, failure):
        """Final answer when a failure routes to an UNDECLARED Aftermath.

        Aftermath is a built-in stage: it always exists, declared or not.
        Undeclared, it has no prompt, so the chain terminates immediately.
        Every final-answer output collected so far is kept, with the
        failure appended last so a partial result cannot be mistaken for
        a success.
        """
        parts = list(outputs or [])
        detail = str(failure or "").strip() or "no further detail"
        parts.append("--- Failure (%s) ---\n\n%s" % (stage_name, detail))
        return "\n\n".join(parts)

    def _hook_stage_failure(self, stage, outcome, turn, fail_target):
        """Turn a failed required hook into the stage's failure routing."""
        message = self._hook_failure_message(outcome)
        self.history.append({"role": "user", "content": message})
        self.ui.display_error(self.config.name, self.agent_id, message)
        return message, "FAIL:%s" % fail_target, turn + 1

    def _call_tool(self, tool_name, args):
        """Invoke a tool map entry, converting exceptions to error strings."""
        try:
            return self.tool_map[tool_name](**args)
        except Exception as e:
            return "Error executing tool '%s': %s" % (tool_name, str(e))

    def _record_blocked_event_signal(self, outcome, events):
        """Report the deferred event signals that a failing hook abandoned.

        Called from BOTH abandonment paths (`posttool` and `preexit`) so the
        runtime's deadlock diagnostic can name the hook that blocked the
        handshake and every event that will now never be signalled - which is
        what makes an unattended multi-agency hang diagnosable instead of
        silent. One record per name, so a multi-event call cannot hide the
        second event behind the first.
        """
        runtime = self.runtime
        if not events or runtime is None:
            return
        if isinstance(events, str):
            events = [events]
        for event in events:
            runtime.record_blocked_signal(
                getattr(self, "agent_id", "agent"),
                outcome.blocked_label,
                outcome.detail,
                event=event,
            )

    def _execute_tool_with_hooks(self, stage, tool_name, args, stage_turn, turn,
                                 fail_target, exits_stage=False):
        """Run one tool call wrapped in pretool/posttool/preexit hooks.

        Returns ``(tool_result, failure_outcome_or_None, ran)``. A non-None
        outcome means a required hook failed and the caller must fail the stage.
        ``ran`` is False only when a `pretool (required)` hook aborted the call
        before it executed, so the caller can keep the "required tools were
        called" bookkeeping honest.

        `ask_user` is excluded from `pretool`/`posttool` only: hooking an open
        interactive prompt would interleave kernel output into the question.
        `preenter`/`preexit` are stage-level and always run, so an `ask_user`
        that a class lists as an Exit Tool still goes through its `preexit`
        gate (a stage gate must not be bypassable by choosing an exit tool).

        `set_event_success` / `set_event_loop` / `set_event_jump` always have
        their events signalled at ONE point - the exit-action step at the
        end - so the
        documented order `pretool -> tool -> posttool -> preexit -> exit action`
        holds regardless of which hook kinds a stage happens to declare, and a
        failing `posttool (required)` / `preexit (required)` can still abandon
        the signal. ALL of the call's event names are deferred and abandoned
        together, and the deferred signal is reported as a blocked signal so the
        runtime's deadlock diagnostic can name each one.
        """
        engine = self._hooks(stage)
        if engine is None:
            return self._call_tool(tool_name, args), None, True

        is_ask_user = tool_name == "ask_user"
        is_event_tool = tool_name in ("set_event_success", "set_event_loop",
                                      "set_event_jump")

        tool_ctx = {
            "name": tool_name,
            "args": args,
            "ok": None,
            "error": None,
            "duration_ms": None,
            "stage_attempt": stage_turn,
        }

        if not is_ask_user:
            pre = engine.run("pretool", stage, tool=tool_ctx, turn=turn)
            self._apply_observations(pre, turn)
            if not pre.ok:
                detail = " ".join(str(pre.detail or "").split())
                return ("Error: pretool hook %s failed for '%s': %s - the tool "
                        "was NOT executed." % (pre.blocked_label, tool_name, detail),
                        None, False)

        started = time.time()
        pending_events = []
        # Always DEFER an event terminator to the exit-action step below, never
        # gated on whether this stage happens to declare a `preexit` hook: the
        # signal must be observable at exactly one place in the documented
        # `pretool -> tool -> posttool -> preexit -> exit action` order, and a
        # failing required `posttool`/`preexit` must still be able to abandon it.
        # EVERY name of the call is deferred and abandoned together.
        if is_event_tool:
            names = self._normalise_event_names(
                self._event_names_from_kwargs(args))
            if tool_name == "set_event_loop":
                # The deferred path never runs the tool impl, so the loop
                # modifier is picked up here instead.
                self._loop_modifier = str(args.get("modifier") or "").strip()
            tool_result = {"set_event_success": "SET_EVENT_SUCCESS_SIGNALED",
                           "set_event_loop": "LOOP_SIGNALED",
                           "set_event_jump": "SET_EVENT_JUMP_SIGNALED"}[tool_name]
            if tool_name == "set_event_jump" and not str(
                    args.get("target") or "").strip():
                tool_result = ("Error: set_event_jump requires a non-empty "
                               "'target' (the stage to move to).")
            error = self._validate_event_names(tool_name, names)
            if error:
                tool_result = error
            elif not tool_result.startswith("Error"):
                pending_events = names
        else:
            tool_result = self._call_tool(tool_name, args)

        text_result = str(tool_result)
        # Hook observation only: `_tool_result_failed` is the documented,
        # case-insensitive form of the tool-failure convention (see its
        # docstring). Stage control keeps its own, unchanged predicate.
        tool_ctx["ok"] = not _tool_result_failed(text_result)
        tool_ctx["error"] = text_result if not tool_ctx["ok"] else None
        tool_ctx["duration_ms"] = int((time.time() - started) * 1000)

        if not is_ask_user:
            post = engine.run("posttool", stage, tool=tool_ctx, turn=turn)
            self._apply_observations(post, turn)
            if not post.ok:
                # The events are still pending, so this failure abandons them.
                self._record_blocked_event_signal(post, pending_events)
                return tool_result, post, True

        if exits_stage:
            exit_check = engine.run("preexit", stage, tool=tool_ctx, turn=turn)
            self._apply_observations(exit_check, turn)
            if not exit_check.ok:
                self._record_blocked_event_signal(exit_check, pending_events)
                return tool_result, exit_check, True

        if pending_events:
            self._set_events(pending_events)
        return tool_result, None, True

    def google_search_tool(self, query):
        return "ERROR: google_search_tool not yet fully initialized."

    def _prompt_interrupt_safe(self, question):
        self._interrupt_suspend()
        try:
            return self.ui.prompt_user(
                self.config.name, self.agent_id, question, multiline=True
            )
        finally:
            self._interrupt_resume()

    def _interrupt_guarded_ui(self):
        """Return the UI wrapped so prompt_user suspends the Esc controller.

        Tool consumers (notably the AccessController security question) ask the
        UI directly, bypassing _prompt_interrupt_safe; without this the active
        cbreak reader can swallow the operator's first keystroke.
        """
        return _PromptGuardUI(
            self.ui, self._interrupt_suspend, self._interrupt_resume
        )

    def _async_registry(self):
        """The async job registry this agent should use.

        Inside a multi-agency run the registry is run-global (owned by the
        runtime, exactly like the endpoints and the bindings), so a job
        started by one agent and a stage parked on its id share one map. A
        single-agent run has no runtime, so the agent keeps its own registry:
        the jobs still run and are still delivered, only a cross-agent
        wait-by-id cannot be satisfied (nothing could set that event).
        """
        runtime = self.runtime
        registry = getattr(runtime, "async_jobs", None) if runtime is not None else None
        if registry is not None:
            return registry
        if self._async_jobs_local is None:
            self._async_jobs_local = AsyncJobRegistry()
        return self._async_jobs_local

    def _drain_async_results(self):
        """Attach every finished, undelivered async job result to history.

        Called once per turn, right before the LLM request, so a background
        command that finished while the agent kept working is delivered on
        the next message instead of waiting for an explicit `poll_async`.
        Returns the number of results attached.
        """
        registry = self._async_registry()
        if registry is None:
            return 0
        finished = registry.drain_finished(owner=self.agent_id)
        for job in finished:
            status = "SUCCESS" if job.status == DONE else "ERROR"
            body = job.output or "(no output)"
            if job.error:
                body = body + chr(10) + "ERROR: " + job.error
            # Echo the command AS THE AGENT WROTE IT: the result arrives after
            # the async_bash call that launched it, and compression may have
            # dropped that call in the meantime. It stays OUTSIDE the fenced
            # body below, so a command containing fences cannot break it.
            command = job.display_cmdline or job.cmdline
            result_msg = ("ASYNC BASH RESULT (" + job.job_id + "):" + chr(10)
                          + "- **Status**: " + status + chr(10)
                          + "- **Command**: " + command + chr(10)
                          + "- **Content**:" + chr(10) + "```" + chr(10)
                          + body + chr(10) + "```")
            self.history.append({"role": "user", "content": result_msg})
            try:
                self.ui.display_tool_result(
                    self.config.name, self.agent_id, "async_bash", body)
            except Exception:
                pass
        return len(finished)

    def _bash_tool_with_guard(self, cmdline):
        """Run bash_tool with a guarded UI for its security question.

        The AccessController asks its permission question (agent id 'ACS')
        through this UI; the guarded proxy suspends the Esc listener during the
        prompt so the operator's first keystroke is not stolen by the active
        cbreak reader.

        The output is also capped to half of the remaining context window,
        resolved here (i.e. at call time) so the budget reflects the history the
        model has actually accumulated; see tools/output_guard.py.
        """
        return bash_tool(
            cmdline,
            access_controller=self.access_controller,
            agent_id=self.agent_id,
            ui=self._interrupt_guarded_ui(),
            max_output_bytes=self._tool_output_budget(),
        )

    def _async_bash_tool_with_guard(self, cmdline):
        """Run async_bash_tool with a guarded UI for its security question.

        Exactly mirrors ``_bash_tool_with_guard``: the AccessController asks
        its permission question (agent id 'ACS') through this UI, and the
        guarded proxy suspends the Esc listener during the prompt so the
        operator's first keystroke is NOT stolen by the active cbreak reader.
        The registry, agent id and output budget are resolved lazily here, at
        call time, and the job is owner-tagged so its result drains back to
        THIS agent only.
        """
        return async_bash_tool(
            cmdline,
            registry=self._async_registry(),
            access_controller=self.access_controller,
            agent_id=self.agent_id,
            ui=self._interrupt_guarded_ui(),
            max_output_bytes=self._tool_output_budget(),
            owner=self.agent_id,
        )

    def ask_user(self, question):
        # ask_user collects natural-language answers which may be long/pasted
        # multi-line text: route it through the chat-style multi-line prompt
        # when a real TTY is available (non-TTY falls back to single-line).
        #
        # Empty/blank replies are NOT a stop/exit signal. Do not silently hand
        # a zero-length answer back to the model: it can invent a stop word
        # (for example a hallucinated 'ya') and prematurely end a prompt-chain
        # stage. Re-prompt a few times so the user can answer; if they remain
        # empty, return an explicit marker that unambiguously records no reply.
        max_empty_attempts = 3
        attempts = 0
        while attempts < max_empty_attempts:
            # Keep the pre-interrupt fallback: an object may only expose
            # _prompt_interrupt_safe on real agents; duck-typed callers use ui.
            safe_prompt = getattr(self, "_prompt_interrupt_safe", None)
            if safe_prompt is not None:
                answer = safe_prompt(question)
            else:
                answer = self.ui.prompt_user(
                    self.config.name, self.agent_id, question, multiline=True
                )
            if str(answer) == ASK_INTERRUPT_SENTINEL:
                action, payload = self._handle_ask_user_interrupt(question)
                if action == "answer":
                    # /resume <text> from inside the venue IS the answer to the
                    # pending question: return it instead of re-prompting.
                    return payload
                if action == "marker":
                    return payload
                # bare /resume or a loop-only session: keep the prompt open.
                continue
            if answer and str(answer).strip():
                return answer
            attempts += 1
            if attempts < max_empty_attempts:
                warning_fn = getattr(self.ui, "display_warning", None)
                if warning_fn is not None:
                    warning_fn(
                        self.config.name,
                        self.agent_id,
                        "Empty reply received. Please type your answer, or use stop/exit wording only if you really want to end this stage.",
                    )
        return "[no reply] (user submitted empty input repeatedly)"

    def _handle_ask_user_interrupt(self, question):
        """Serve the diagnose venue when it is opened from inside ask_user.

        Returns (action, payload):
          * ('answer', text) - /resume with text: the text IS the answer to the
            pending question, so ask_user returns it directly.
          * ('marker', text) - a chain move (rewind / jump / restart / quit)
            was requested; record it as the deferred status and return a tool
            marker so the ReAct loop can move the chain.
          * ('reask', None)  - bare /resume or a loop-only session; re-open the
            same question.
        """
        result = self._serve_interrupt_venue(
            {"name": "ask_user"}, 0, set(), entry="ask_user"
        )
        if result is None:
            return ("reask", None)
        if result.status == ic.STATUS_RESUME:
            if result.text:
                return ("answer", result.text)
            return ("reask", None)
        if (
            result.status in (ic.STATUS_REWIND, ic.STATUS_RESTART)
            or result.status.startswith(ic.STATUS_JUMP)
            or result.status.startswith(ic.STATUS_FAIL)
        ):
            self._pending_interrupt_status = result.status
            label = ic.label_for_status(result.status) or result.status
            return ("marker", f"[interrupt: {label} requested]")
        return ("reask", None)


    def validate_response(self, response):
        """Check for <tool_code> format and handle potential malformations."""
        # Patterns for start and end of tool code blocks, including DSML and hallucinated variations
        # We catch standard tags, and common leaks like <｜｜DSML｜｜tool_code> or ｜｜tool_code>
        start_patterns = list(_TOOL_BLOCK_START_PATTERNS)
        end_patterns = list(_TOOL_BLOCK_END_PATTERNS)
        
        if not isinstance(response, str) or not response.strip():
            got = type(response).__name__ if not isinstance(response, str) else 'an empty reply'
            return None, (
                'Malformed response: expected a non-empty string reply, got %s.' % got)

        # Robustly handle reasoning section - identify what's inside and outside
        content_outside_reasoning = response
        if "<reasoning>" in response:
            if "</reasoning>" in response:
                content_outside_reasoning = re.sub(r'<reasoning>.*?</reasoning>', '', response, flags=re.DOTALL)
            else:
                # Unclosed tag, treat everything after as reasoning
                content_outside_reasoning = response.split("<reasoning>", 1)[0]

        # A fabricated observation must never leave the model: only the framework
        # writes the envelope; text inside a call block is judged separately.
        if _looks_like_fabricated_observation(_strip_tool_blocks(content_outside_reasoning)):
            return None, _FABRICATED_OBSERVATION_ERROR

        # Find all potential tool block starts in content_outside_reasoning
        all_starts = []
        for p in start_patterns:
            for m in re.finditer(p, content_outside_reasoning, re.IGNORECASE):
                all_starts.append(m.end())
        
        if all_starts:
            # Prioritize the last occurrence to be robust against redundant leading tags
            start_idx = sorted(all_starts)[-1]
            starts = sorted(set(all_starts))
            if len(starts) > 1:
                distinct = _distinct_tool_calls(content_outside_reasoning, starts, end_patterns)
                if len(distinct) > 1:
                    return None, (
                        'Malformed tool call: this reply carries %d different tool calls. '
                        'Emit exactly ONE call per turn so the framework cannot act on the '
                        'wrong one.' % len(distinct))

            remaining_content = content_outside_reasoning[start_idx:]
            
            parsed_tool, last_err = _parse_tool_block(remaining_content, end_patterns)

            if parsed_tool is not None:
                return _validate_tool_call_shape(parsed_tool)

            return None, f"Malformed tool block: The content inside the tool block is not a valid JSON object with a 'tool' key. Error: {last_err or 'invalid JSON structure'}"
        
        # If we reached here, no valid tool call was found using patterns.
        # Now perform a fallback/error check for DSML leakage outside reasoning.
        dsml_pattern = r'<[^>]*DSML[^>]*>|[|｜]{2,}DSML[|｜]{2,}'
        if re.search(dsml_pattern, content_outside_reasoning, re.IGNORECASE):
             # DeepSeek flash models may speak their native DSML tool-call
             # dialect; interpret it instead of bouncing the turn. Every other
             # model keeps the hard malformed-call error below.
             if getattr(self, '_dsml_accommodate', False):
                 dsml_tool = _parse_dsml_invoke(content_outside_reasoning)
                 if dsml_tool is not None:
                     return _validate_tool_call_shape(dsml_tool)
             return None, "Malformed tool call: You used internal reasoning markers (like DSML or ｜｜) outside the reasoning block. Please use exactly <tool_code>...</tool_code> for tool calls."

        # Check for mismatched tags (only if has_end but no has_start, which is rare but possible)
        if "</tool_code>" in content_outside_reasoning:
            return None, "Malformed tool call: found </tool_code> but no <tool_code>."

        # Check for close-enough tags (common LLM typos like </tool_cod> or <tool-code>)
        # We look for <tool_ or </tool_ followed by anything ending in >
        fuzzy_match = re.search(r'<(?:/)?tool[_-]cod[^>]*>', content_outside_reasoning, re.IGNORECASE)
        if fuzzy_match:
             return None, f"Malformed tool call detected near '{fuzzy_match.group(0)}'. Please use exactly <tool_code> and </tool_code>."

        # If we reached here, there is no tool call. 
        # Now check if the response was ONLY reasoning.
        if not content_outside_reasoning.strip() and "<reasoning>" in response:
             return None, "Malformed response: Your output contains only reasoning. Please provide a tool call or a final answer outside the <reasoning> block."

        return None, None 

    def _observation_error(self, response, check_claims=False):
        '''Return an error when the reply fabricates an observation.

        The envelope check is format-based. The claim check only applies where a
        turn may end as a final answer, and only fires when the agent has never
        received a real observation - otherwise a legitimate wrap-up that
        mentions earlier results would be rejected.
        '''
        prose = ''
        if isinstance(response, str):
            prose = _strip_tool_blocks(_strip_reasoning(response))
        if _looks_like_fabricated_observation(prose):
            return _FABRICATED_OBSERVATION_ERROR
        if (check_claims and _VERIFIED_CLAIM_RE.search(prose)
                and not self._has_real_observation()):
            return _UNGROUNDED_CLAIM_ERROR
        return None

    def _has_real_observation(self):
        '''True when the history already holds a framework observation envelope.'''
        for msg in getattr(self, 'history', None) or []:
            content = msg.get('content') if isinstance(msg, dict) else None
            if isinstance(content, str) and content.startswith(_TOOL_RESULT_PREFIXES):
                return True
        return False

    def restart_impl(self):
        if self.restart_count < ChainBudget.RESTART_CAP:
            return "RESTART_SIGNALED"
        return ("Error: Maximum restart limit reached (%s)."
                % ChainBudget.RESTART_CAP)

    def loop_tool_impl(self, summary="", modifier=""):
        """Loop the CURRENT stage: end it and immediately re-enter it.

        `summary` is the RESULT OF THIS STAGE (its final response, exactly
        as in success_tool). `modifier` is a one-shot note appended to the
        re-entered stage's prompt as a [TASK MODIFIER] section. To move to a
        DIFFERENT stage, use exit_to / set_event_jump instead.
        """
        self._loop_modifier = str(modifier or "").strip()
        return "LOOP_SIGNALED"

    # --- exit_to: chain moves -------------------------------------------

    EXIT_TO_CALL_DEPTH_CAP = 16

    def exit_to_impl(self, name=None):
        """Leave the current stage and continue the chain from `name`."""
        target = str(name or "").strip()
        if not target:
            return ("Error: exit_to requires a non-empty 'name' (the stage "
                    "to move to).")
        return "EXIT_TO_SIGNALED"

    def _resolve_exit_to(self, target):
        """Resolve an exit_to target to ("stage"|"call", canonical name).

        "stage" means the name is a stage of the ordinary prompt chain (a
        positional jump); "call" means it is a declared but UNATTACHED
        (Detached) stage (a call). Case-insensitive, matching
        `_resolve_stage_index`. Returns None when neither matches.
        """
        wanted = str(target or "").strip().lower()
        if not wanted:
            return None
        for s in (self.config.prompt_chain or []):
            if str(s.get("name", "")).strip().lower() == wanted:
                return ("stage", str(s.get("name")))
        for dname in (self.config.detached_stages or {}):
            if str(dname).strip().lower() == wanted:
                return ("call", str(dname))
        return None

    def _clear_call_frame(self):
        """Forget any exit_to call frame (the walk is back in the chain)."""
        self._exit_to_call = None
        self._exit_to_depth = 0
        self._exit_to_return = None

    def _walk_advance(self, idx):
        """The next walk index after a stage completes.

        Normally idx + 1. When the stage that just completed was an exit_to
        CALL, the call nest unwinds instead: because exit_to TERMINATES the
        calling stage, a callee that itself calls another callee never needs to
        resume itself, so the whole nest always returns to the ONE chain index
        recorded when the nest was entered (the caller's next stage).
        """
        if self._exit_to_call is None:
            return idx + 1
        self._exit_to_call = None
        self._exit_to_depth = 0
        nxt = self._exit_to_return
        self._exit_to_return = None
        return idx if nxt is None else nxt


    def _exit_tools_used(self):
        """Return the set of exit tools referenced across all prompt-chain stages."""
        tools = set()
        for stage in self.config.prompt_chain or []:
            for t in stage.get("exit_tools", []) or []:
                tools.add(t)
        return tools

    def run(self, task_prompt, seed_history=None):
        # Synchronous entry point: drive the cooperative chain to the end.
        # Multi-agency runs are driven by AgentRuntime; this wrapper keeps
        # single-agent behaviour identical, and raises if the chain blocks on
        # an event that no other agent can ever set.
        gen = self.iter_run(task_prompt, seed_history=seed_history)
        try:
            while True:
                try:
                    token = next(gen)
                except StopIteration as stop:
                    return stop.value
                if token and token[0] == "wait":
                    raise RuntimeDeadlockError(
                        "Agent '%s' is waiting on event '%s' in a single-agent run; "
                        "no other agent can set it." % (self.config.name, token[1])
                    )
        finally:
            # The chain is over: release the stage-hook python kernel, if any.
            self._shutdown_hooks()

    def iter_run(self, task_prompt, seed_history=None):
        # Cooperative coroutine for the whole prompt chain: yields
        # ("enter", stage_name) at each stage boundary and ("wait", event_name)
        # when a stage is gated by an unset Wait Event. The StopIteration value
        # is the final answer.
        self.logger.set_task(task_prompt)
        self.restart_count = 0
        self.loop_count = 0
        
        # Initialize history ONCE for the entire task. A cloned spawn seeds the
        # child with the parent's context snapshot; the child's own system
        # prompt still heads the window.
        system_prompt = self.build_system_prompt()
        if seed_history:
            seeded = [dict(m) for m in seed_history]
            if seeded and seeded[0].get("role") == "system":
                seeded[0] = {"role": "system", "content": system_prompt}
            else:
                seeded = [{"role": "system", "content": system_prompt}] + seeded
            self.history = seeded
        else:
            self.history = [{"role": "system", "content": system_prompt}]
        self.logger.log_turn(0, self.history) # Log system prompt immediately
        turn = 1
        
        current_task_prompt = task_prompt
        self.current_task_prompt = current_task_prompt
        
        # Restart and loop accounting, and the Max-Loops rule, live in
        # core/chain.py; the agent keeps only a mirror of the two counters for
        # the /status dump and for the operator-facing rule below.
        budget = ChainBudget(self.loop_limit)
        while budget.can_start_pass():
            chain = self.config.prompt_chain
            if not chain:
                # Fallback to pure task prompt if no chain is defined
                chain = [{"name": "Task", "content": ""}]

            restart_triggered = False
            final_answer_outputs = []
            # Guard against a required preenter hook bouncing the chain between
            # failure-handling stages forever. This counts re-routings WITHIN one
            # chain pass and is reset at the start of each pass, so a long-lived
            # agent that legitimately recovers many times is not aborted.
            preenter_routing_failures = 0

            # The walk OWNS the index, the exit_to call frame and the runaway
            # counter (see core/chain.py). Building a FRESH walk per pass is what
            # makes a previous pass unable to leave a stale call frame behind.
            walk = ChainWalk(chain)
            while not walk.at_end():
                stage = walk.stage
                runaway = walk.enter()
                if runaway:
                    return runaway
                # Publish the chain position for the venue /status dump.
                self._chain_index = walk.idx
                self._chain_len = walk.stage_count

                # Cross-agent synchronisation happens ONLY here, at the stage
                # boundary: a stage is not entered until its Wait Event has
                # been CONSUMED. consume_event() takes a latched pulse (the
                # producer ran early) and clears it; otherwise the agent parks.
                # The runtime re-queues a parked handle only from set_event, so
                # a RESUME means the wait was satisfied - never re-check here,
                # because the pulse has already been consumed (auto-reset).
                wait_event = stage.get("wait_event")
                entry_failure = None
                if wait_event:
                    # Late binding: a placeholder is resolved through the
                    # run-global binding registry. UNBOUND means the wait can
                    # never be satisfied, so the stage is NOT entered and the
                    # failure routes to the stage's Fail Target below, using the
                    # same target and lookup rules as the `preenter` routing.
                    wait_event, entry_failure = self._resolve_event_name(wait_event)
                    if entry_failure is None and is_ep(wait_event):
                        # A REMOTE endpoint is a LEVEL signal over a queue, so it
                        # is READ, not consumed: take one message and clear
                        # nothing. The stage prompt is then told what arrived.
                        resolved = yield from self._await_endpoint(wait_event)
                        wait_event, entry_failure, notice = resolved
                        if entry_failure is None:
                            self._remote_notice = notice
                    elif entry_failure is None:
                        if self.runtime is None:
                            # Single-agent run: nothing can ever set it, so park
                            # once and let the synchronous drain wrapper raise.
                            yield ("wait", wait_event)
                        elif not self.runtime.consume_event(wait_event):
                            yield ("wait", wait_event)
                if entry_failure is None:
                    yield ("enter", stage["name"])
                else:
                    self.history.append({"role": "user", "content": entry_failure})
                    self.ui.display_error(self.config.name, self.agent_id,
                                          entry_failure)
                    if str(stage.get("name", "")).strip().lower() == "aftermath":
                        self.logger.log_turn(turn, self.history)
                        return self._fail_termination_answer(
                            final_answer_outputs, stage.get("name"),
                            entry_failure)
                    target_idx = walk.target_index(
                        self._stage_failure_target(stage), None)
                    if target_idx is None:
                        target_idx = walk.target_index("aftermath", None)
                        if target_idx is None:
                            self.logger.log_turn(turn, self.history)
                            return self._fail_termination_answer(
                                final_answer_outputs, stage.get("name"),
                                entry_failure)
                    if target_idx == walk.idx:
                        return ("%s The stage's fail target is the stage itself "
                                "(%s), so there is nothing to route to; aborting."
                                % (entry_failure, stage.get("name")))
                    walk.clear_call()
                    walk.idx = target_idx
                    continue

                # Stage hooks: `preenter` runs AFTER the wait has been satisfied
                # and BEFORE the stage prompt is built, so a (capture) hook can
                # inform the model before it plans. A failing required preenter
                # hook blocks entry to this stage and routes through the stage's
                # normal failure path.
                preenter = self._hooks(stage)
                if preenter is not None:
                    preenter_outcome = preenter.run("preenter", stage, turn=turn)
                    self._apply_observations(preenter_outcome, turn)
                    if not preenter_outcome.ok:
                        # Same target and same lookup as the tool path
                        # (`FAIL:` handling below): only the wording helper and
                        # these two helpers are shared, so the two routing paths
                        # cannot drift.
                        failure_target = self._stage_failure_target(stage)
                        message = self._hook_failure_message(preenter_outcome)
                        self.history.append({"role": "user", "content": message})
                        self.ui.display_error(self.config.name, self.agent_id, message)
                        if str(stage.get("name", "")).strip().lower() == "aftermath":
                            # Aftermath is the terminal stage: a failure there
                            # ends the chain, with the failure recorded after
                            # the curated outputs (same rule as the `FAIL:`
                            # routing below).
                            self.logger.log_turn(turn, self.history)
                            return self._fail_termination_answer(
                                final_answer_outputs, stage.get("name"),
                                message)
                        target_idx = walk.target_index(failure_target, None)
                        if target_idx is None:
                            # Built-in Aftermath, undeclared: no prompt, so
                            # the chain terminates immediately (same rule as
                            # the `FAIL:` routing below).
                            target_idx = walk.target_index("aftermath", None)
                            if target_idx is None:
                                self.logger.log_turn(turn, self.history)
                                return self._fail_termination_answer(
                                    final_answer_outputs, stage.get("name"),
                                    message)
                        if target_idx == walk.idx:
                            # NB: returning from `iter_run` ends the chain, and
                            # the returned value IS the chain's final answer
                            # (`Agent.run` returns the generator's stop value;
                            # the runtime stores it as the handle result). It is
                            # deliberately a bare string, never a
                            # `(response, status, turn)` tuple.
                            return ("%s The stage's fail target is the stage "
                                    "itself (%s), so there is nothing to route "
                                    "to; aborting."
                                    % (message, stage.get("name")))
                        preenter_routing_failures += 1
                        if preenter_routing_failures > 5:
                            return ("Error: too many preenter hook failures "
                                    "(%d); aborting the chain."
                                    % preenter_routing_failures)
                        walk.clear_call()
                        walk.idx = target_idx
                        continue

                prompt = stage["content"]
                if walk.idx == 0 and not walk.in_call:
                    # The first stage combines with the dynamic task prompt
                    prompt = (prompt + "\n\n" + current_task_prompt).strip()
                if self._loop_modifier:
                    # One-shot: a loop_tool / set_event_loop modifier rides
                    # THIS entry only, as a [TASK MODIFIER] section.
                    prompt = (prompt + "\n\n[TASK MODIFIER]: "
                              + self._loop_modifier).strip()
                    self._loop_modifier = ""
                if self._remote_notice:
                    # One-shot: a remote message/connection notice rides THIS
                    # entry only, then clears.
                    prompt = (prompt + "\n\n" + self._remote_notice).strip()
                    self._remote_notice = ""

                if not prompt:
                    walk.idx = walk.advance()
                    continue

                # Inject allowed-tools note if the stage restricts tools
                allowed_tools = stage.get("allowed_tools")
                if allowed_tools:
                    required_tools = stage.get("required_tools", []) or []
                    exit_tools = stage.get("exit_tools", []) or []
                    effective = sorted(set(allowed_tools) | set(required_tools) | set(exit_tools))
                    prompt += f"\n\n[STAGE CONSTRAINT] You may ONLY call the following tools in this stage: {', '.join(effective)}. Calling any other tool will result in an error."

                # Tell the TUI which prompt-chain stage is active so section
                # headers read `Agent (id) : <Stage> : <Action>`.
                self.ui.set_stage_name(stage['name'])
                # A fresh stage entry starts a fresh signal ledger for the SNAP MARKER.
                self._stage_signalled_events = set()

                # Display stage transition if multiple stages
                if walk.stage_count > 1:
                    self.ui.display_stage_transition(self.config.name, self.agent_id, stage['name'])

                # Add prompt to history
                self.history.append({"role": "user", "content": prompt})
                self.logger.log_turn(turn, self.history) # Log user prompt immediately

                # _run_react_loop returns (final_response, status, next_turn)
                final_response, status, turn = self._run_react_loop(turn, stage, prompt)

                # Stage-boundary interrupt poll: drain a pending Esc before the
                # status routing, so it is serviced against THIS stage's context
                # instead of leaking into the next stage. Assigning `status`
                # reuses the existing routing below.
                boundary = self._maybe_boundary_venue(stage)
                if boundary is not None:
                    if boundary.text:
                        self.history.append({
                            "role": "user",
                            "content": f"[USER INTERRUPT: {boundary.text}]",
                        })
                        self.logger.log_turn(turn, self.history)
                    if boundary.status != ic.STATUS_RESUME:
                        status = boundary.status

                # Formal snap boundary: a stage that completed may opt in to
                # context compression at stage end. Runs before the next stage
                # or before a chain loop/new-task transition.
                if status in ("DONE", "LOOP", "RESTART"):
                    turn = self._maybe_snap(stage, turn)

                # Curate the stage result BEFORE routing: a stage that
                # COMPLETED - DONE, LOOP or EXIT_TO (which set_event_jump also
                # produces) - refreshes the final answer; FAIL / RESTART /
                # ERROR / interrupt outcomes never do.
                if (status in (StageExit.DONE, StageExit.LOOP)
                        or StageExit.is_exit_to(status)):
                    self.last_final_answer = final_response
                    if stage.get("final_answer"):
                        label = stage["name"]
                        if walk.idx == 0 and not walk.in_call:
                            label = f"{label} and task"
                        final_answer_outputs.append(
                            f"--- {label} ---\n\n{final_response}")

                if status == StageExit.RESTART:
                    restart_triggered = True
                    break
                elif status == StageExit.LOOP:
                    # Loop re-enters the SAME stage: the walk does NOT advance,
                    # so the loop-top Wait Event gate is re-checked on re-entry
                    # (a looped stage parks again exactly like a fresh entry).
                    # The modifier, if any, was stashed by the tool impl and is
                    # consumed when the prompt is rebuilt. Costs one chain
                    # operation against Max Loops.
                    if not budget.charge_operation():
                        # The refused operation still counts (the mirror must
                        # show the attempt that tripped the cap).
                        self.loop_count = budget.loop_count
                        return budget.loops_answer()
                    self.loop_count = budget.loop_count
                    continue
                elif status == StageExit.ERROR:
                    return final_response
                elif status == StageExit.INTERRUPT_REWIND:
                    # Re-enter the current stage without advancing the walk. The
                    # outer loop re-reads the same stage and appends the prompt
                    # again, preserving old history/side effects.
                    continue
                elif StageExit.is_interrupt_jump(status):
                    target = StageExit.interrupt_jump_target(status)
                    matched = walk.jump_index(target)
                    if matched is None:
                        self.ui.display_warning(
                            self.config.name,
                            self.agent_id,
                            f"Interrupt jump target '{target}' not found in chain; resuming current stage.",
                        )
                        continue
                    walk.clear_call()
                    walk.idx = matched
                    continue
                elif StageExit.is_exit_to(status):
                    resolved = self._resolve_exit_to(
                        StageExit.exit_to_target(status))
                    if resolved is None:
                        # The ReAct loop validated the target BEFORE leaving the
                        # stage, so this is unreachable; advance rather than
                        # move somewhere arbitrary.
                        self.ui.display_warning(
                            self.config.name, self.agent_id,
                            "exit_to target vanished; advancing normally.")
                        walk.clear_call()
                        walk.idx = walk.advance()
                        continue
                    target_kind, canonical = resolved
                    # Max Loops counts ANY operation on the chain: a loop, a
                    # jump and a call each cost one.
                    if not budget.charge_operation():
                        self.loop_count = budget.loop_count
                        return budget.loops_answer()
                    self.loop_count = budget.loop_count
                    if target_kind == "stage":
                        # A positional jump - NOT a loop (no modifier) and the
                        # first stage is not special. A jump abandons any call in progress.
                        walk.clear_call()
                        walk.idx = walk.jump_index(canonical)
                        continue
                    # CALL to a declared but unattached (Detached) stage: run it
                    # as a full stage, then resume at the caller's next stage.
                    error = walk.push_call(canonical, self.config.detached_stages)
                    if error:
                        return error
                    continue
                elif StageExit.is_fail(status):
                    target = StageExit.fail_target(status)
                    if str(stage.get("name", "")).strip().lower() == "aftermath":
                        # Aftermath is the terminal stage and has NO fail
                        # target: a failure there (fail_tool or a failed
                        # required hook) ends the chain, with the failure
                        # recorded after the curated outputs.
                        self.logger.log_turn(turn, self.history)
                        return self._fail_termination_answer(
                            final_answer_outputs, stage.get("name"),
                            final_response)
                    # Shared with the `preenter` routing (see ChainWalk.
                    # target_index) so both resolve a target identically.
                    target_idx = walk.target_index(target, None)
                    if target_idx is None:
                        # Aftermath is a built-in stage: an unresolved fail
                        # target routes there by convention. Undeclared, it
                        # has no prompt, so the chain TERMINATES immediately -
                        # never fall back to the last stage, which may be the
                        # current one (the zero-progress hang).
                        target_idx = walk.target_index("aftermath", None)
                        if target_idx is None:
                            self.logger.log_turn(turn, self.history)
                            return self._fail_termination_answer(
                                final_answer_outputs, stage.get("name"),
                                final_response)
                    walk.clear_call()
                    if target_idx == walk.idx:
                        return ("Error: stage '%s' failed and its fail "
                                "target resolves to the stage itself "
                                "(%s); aborting instead of looping."
                                % (stage.get("name"), target))
                    walk.idx = target_idx
                    continue

                # Fall-through: the stage completed (DONE) - advance the walk.
                walk.idx = walk.advance()

            if not restart_triggered:
                # Finished entire chain successfully
                if final_answer_outputs:
                    final_answer = "\n\n".join(final_answer_outputs)
                else:
                    final_answer = self.last_final_answer

                # Ensure the final returned answer is recorded in the log
                if final_answer and (not self.history or self.history[-1].get("content") != final_answer):
                    self.history.append({"role": "assistant", "content": final_answer})
                    self.logger.log_turn(turn, self.history)
                else:
                    # Even if already in history, make sure the last turn is flushed
                    self.logger.log_turn(turn, self.history)

                return final_answer
            
            if restart_triggered:
                # Restart loop: History is PRESERVED
                budget.note_restart()
                self.restart_count = budget.restart_count
                self.ui.console.print()
                self.ui.console.rule(f"[bold red]RESTARTING CHAIN ({budget.restart_count}/{ChainBudget.RESTART_CAP}) - PRESERVING CONTEXT[/bold red]", style="bold red")

        return budget.exhausted_answer()

    def _retry_is_unattended(self):
        """Return True when this run should be treated as unattended.

        An explicit ``unattended`` flag wins. Otherwise infer from stdin: a
        non-TTY/redirected process is unattended; a TTY process is attended
        even when the operator is slow to reply.
        """
        if self.unattended is not None:
            return bool(self.unattended)
        try:
            return not sys.stdin.isatty()
        except Exception:
            return True

    def log_error(self, category, message, **kwargs):
        """Record a silently recovered error to the session error log.

        Duck-typed and non-fatal on purpose: partial test doubles may not carry
        ``logger.log_error``, and a logging problem must never break a run.

        ``model`` and ``provider`` are attached here, because the logger holds no
        agent config, so every entry names the model that misbehaved. They ride in
        ``extra`` to keep ``AgentLogger.log_error``'s frozen signature intact; an
        unknown value is still emitted (as ``-``), so the field set never varies.
        A caller-supplied ``extra`` wins over both, and a non-mapping ``extra``
        is handed straight through for the logger to render.
        """
        logger = getattr(self, 'logger', None)
        fn = getattr(logger, 'log_error', None)
        if fn is None:
            return
        try:
            raw_extra = kwargs.pop('extra', None)
            if raw_extra is not None and not hasattr(raw_extra, 'items'):
                kwargs['extra'] = raw_extra
                fn(category, message, **kwargs)
                return
            extra = dict(raw_extra or {})
            extra.setdefault('model', getattr(getattr(self, 'config', None), 'model', None))
            extra.setdefault('provider', getattr(getattr(self, 'model_cfg', None), 'provider', None))
            fn(category, message, extra=extra, **kwargs)
        except Exception:
            return

    def _prompt_retry_wait_or_break(self, stage_name, failure_hint):
        """Ask the user after one retry round is fully exhausted.

        Returns:
            "break": user explicitly chose to stop gracefully.
            "wait": user chose to wait for another round.
            None: no reply (timed out / EOF / non-TTY) — caller starts another round.
        """
        question = (
            f"YAHA reached its retry limit for stage '{stage_name}' "
            f"after {self.llm_retry_limit} attempts.\n\n"
            f"Last failure: {str(failure_hint)[:300]}\n\n"
            "Do you want to wait for another retry round, or break?\n"
            "Type 'wait' (or press Enter) to retry later; "
            "type 'break'/'quit'/'abort' to stop."
        )
        self.ui.display_warning(
            self.config.name,
            self.agent_id,
            f"API/LLM calls keep failing after {self.llm_retry_limit} attempts.",
        )
        try:
            # Suspend the Esc listener for the wait/break prompt too: it asks the
            # UI directly, so an active cbreak reader could eat the first key.
            self._interrupt_suspend()
            try:
                answer = self.ui.prompt_user(
                    self.config.name,
                    self.agent_id,
                    question,
                    default="wait",
                    timeout=self.api_retry_prompt_timeout,
                )
            finally:
                self._interrupt_resume()
        except KeyboardInterrupt:
            # User pressed Ctrl+C while the timed prompt was waiting -> break.
            return "break"
        except Exception:
            answer = None
        if answer is None:
            return None
        normalized = str(answer).strip().lower()
        if normalized in {"break", "b", "quit", "abort", "stop", "exit", "no", "n", "q"}:
            return "break"
        return "wait"

    # --- User interrupt helpers -------------------------------------------
    def _interrupt_suspend(self):
        # The engine-owned controller holds stdin in cbreak with a live reader
        # thread. Suspend it so this agent's prompt gets exclusive stdin
        # ownership; a no-op when no controller is installed (non-TTY runs).
        if self.stdin_interrupt is not None:
            self.stdin_interrupt.suspend()

    def _interrupt_resume(self):
        if self.stdin_interrupt is not None:
            self.stdin_interrupt.resume()

    def _quit_target(self, stage):
        """The stage /quit routes to (Aftermath when there is no real stage)."""
        name = str((stage or {}).get('name', '')).strip().lower()
        if not name or name == 'ask_user':
            return 'aftermath'
        return self._stage_failure_target(stage)

    def _venue_status_report(self, stage, stage_turn, tools_called):
        """Ground-truth runtime dump for /status.

        Every line is read from live state and the model is never involved: that
        is the one thing the diagnostic chat cannot provide, because a model's
        answer about its own state can be stale or invented.
        """
        stage = stage or {}
        name = stage.get('name', 'Stage')
        tokens = getattr(self, '_last_prompt_tokens', 0) or 0
        window = self.context_size or 0
        pct = f' ({round(100.0 * tokens / window)}%)' if window else ''
        allowed = sorted(stage.get('allowed_tools', []) or [])
        required = sorted(stage.get('required_tools', []) or [])
        exit_tools = sorted(stage.get('exit_tools', []) or [])
        chain_pos = ''
        if self._chain_len:
            chain_pos = f'  chain {self._chain_index + 1}/{self._chain_len}'
        last_msg = 'none'
        if self.history:
            last = self.history[-1]
            body = str(last.get('content') or '')
            if body.startswith('TOOL RESULT ('):
                last_msg = 'tool result'
            else:
                last_msg = str(last.get('role') or '?')
        last_tool = 'none'
        if self._last_tool_result:
            last_tool = str(self._last_tool_result[0]) + ' -> ' + str(self._last_tool_result[1])
        caller = self.caller_id if self.caller_id else '- (root)'
        session = getattr(self, 'session', None)
        workspace = str(getattr(session, 'workspace_dir', '') or '').strip() or '(unknown)'
        raw_log = str(getattr(getattr(self, 'logger', None), 'log_path', '') or '').strip()
        if raw_log and not os.path.isabs(raw_log) and workspace != '(unknown)':
            # session.log_dir is relative, so anchor it for an actionable path.
            log_path = os.path.join(workspace, raw_log)
        else:
            log_path = raw_log or '(none)'
        ctl = self.stdin_interrupt
        esc_pending = ctl.is_pending() if ctl is not None else False
        listener_line = (
            f'listener    active  cbreak={ctl.cbreak_now}'
            if ctl is not None and ctl.active else 'listener    absent'
        )
        snap = getattr(self, '_snap_marker', None)
        if snap:
            snap_line = ('snap        %s stage=%s phase=%s signalled=%s'
                         % (snap.get('when'), snap.get('stage'),
                            snap.get('phase'), snap.get('signalled') or 'none'))
        else:
            snap_line = 'snap        none'
        lines = [
            'runtime truth (no model involved)',
            f'agent       {self.config.name}  id={self.agent_id}  caller={caller}',
            f'workspace   {workspace}',
            f'log         {log_path}',
            f'venue       entered from {self._venue_entry or "loop"}  stage={name}{chain_pos}',
            f'turns       global {self._current_turn}  stage {stage_turn}',
            f'tools       called here: {sorted(tools_called) or "none"}',
            f'            allowed: {allowed or "all"}',
            f'            required: {required or "-"}   exit: {exit_tools or "-"}',
            f'model       {self.config.model}  ctx {tokens}/{window}{pct}  compressions {self.compress_count}',
            f'history     {len(self.history)} messages   last: {last_msg}',
            f'last tool   {last_tool}',
            f'interrupt   pending={esc_pending}  deferred={self._pending_interrupt_status or "none"}  serving={self._serving_interrupt}  compressing={self._compressing}',
            snap_line,
            listener_line,
            f'retry       limit {self.llm_retry_limit}/round  unattended max {self.api_retry_unattended_max_rounds}',
        ]
        return '\n'.join(lines)

    def _venue_chat(self, question, transcript):
        """Answer one operator question from inside the venue.

        Runs a single LLM turn (the same provider path as the normal loop) over a
        COPY of the live history plus the venue-local transcript. Chain history
        is never mutated here, and any failure is reported as the answer so the
        venue always survives.
        """
        messages = []
        framing = (
            '\n\n[INTERRUPT VENUE] The operator paused the run at the interrupt '
            'stage and is asking diagnostic questions. Answer them about the '
            'current state concisely; you are not executing a task right now.'
        )
        for idx, msg in enumerate(list(self.history or [])):
            if idx == 0 and msg.get('role') == 'system':
                messages.append({'role': 'system', 'content': str(msg.get('content') or '') + framing})
            else:
                messages.append(dict(msg))
        for turn in transcript:
            messages.append(dict(turn))
        messages.append({'role': 'user', 'content': str(question)})
        try:
            with self.ui.spinner('Interrupt: asking the model...'):
                answer, usage, _ = self.llm.generate(messages, context_size=self.context_size)
        except Exception as exc:
            return f'[chat failed: {exc}]'
        if usage and usage.get('prompt_tokens'):
            # The venue measured a COPY of the history plus its own transcript, so
            # baseline the anchor on that prompt's characters, not the history's.
            self._record_usage(
                usage,
                prompt_chars=sum(len(m.get('content') or '') for m in messages),
            )
        text = str(answer or '').strip()
        if not text:
            return '[chat failed: empty response]'
        transcript.append({'role': 'user', 'content': str(question)})
        transcript.append({'role': 'assistant', 'content': text})
        return text

    def _run_direct_detached_interrupt(self, stage_name, request_text):
        stage = (self.config.detached_stages or {}).get(stage_name)
        if not stage:
            return None
        content = (stage.get('content') or '').strip()
        prompt = content + '\n\nUser interrupt call request: ' + str(request_text or '')
        try:
            messages = [
                {'role': 'system', 'content': self.soul_prompt or self.config.system_prompt or ''},
                {'role': 'user', 'content': prompt},
            ]
            response, usage, _ = self.llm.generate(messages, context_size=self.context_size)
            if response and response.strip():
                return response.strip()[:400]
        except Exception as e:
            return f'error running detached stage {stage_name}: {e}'
        return None

    def _run_spawn_interrupt(self, agent_class, task_text):
        spawn = getattr(self, 'yaha_spawn_agent', None)
        if spawn is None:
            return None
        try:
            result = spawn(system=self.soul_prompt or '', instruction=str(task_text or ''), agent_class=agent_class)
            return str(result)[:400]
        except Exception as e:
            return f'error spawning {agent_class}: {e}'

    def _resolve_stage_index(self, chain, target):
        """Return index of attached stage whose name matches target, else None."""
        if target is None:
            return None
        wanted = str(target).strip().lower()
        for j, s in enumerate(chain or []):
            if str(s.get('name', '')).strip().lower() == wanted:
                return j
        return None

    def _venue_call(self, arg):
        """Run a detached stage from the venue and show its output."""
        stage_name, _, extra = str(arg).partition(' ')
        digest = self._run_direct_detached_interrupt(stage_name, extra.strip())
        if not digest:
            self.ui.display_warning(
                self.config.name, self.agent_id,
                f"Detached stage '{stage_name}' produced no output.",
            )
            return
        self.ui.interrupt_answer(self.config.name, self.agent_id, digest)

    # --- venue inspection helpers (read-only, model-free) -------------------

    def _venue_history_page(self, page, page_size):
        """Render one page of the venue history browser.

        Returns (text, page, pages). Pages are ordered oldest -> newest.
        """
        total = len(self.history)
        try:
            page_size = max(1, min(100, int(page_size)))
        except (TypeError, ValueError):
            page_size = 10
        if total == 0:
            return ('history: 0 messages' + chr(10) + 'nav: q=quit', 1, 1)
        pages = (total + page_size - 1) // page_size
        page = max(1, min(int(page), pages))
        start = (page - 1) * page_size
        end = min(total, start + page_size)
        lines = ['history: %d messages  (page %d/%d)  showing [%d-%d]'
                 % (total, page, pages, start, end - 1)]
        for idx in range(start, end):
            msg = self.history[idx]
            role = str(msg.get('role') or '?')
            body = str(msg.get('content') or '').replace(chr(10), ' ').strip()
            if len(body) > 100:
                body = body[:97] + '...'
            lines.append('  [%d] %s: %s' % (idx, role.ljust(9), body))
        lines.append('nav: n=next page  p=prev page  g <page>  d <idx>=detail  q=quit')
        return (chr(10).join(lines), page, pages)

    def _venue_history_browser(self, arg):
        """Interactive, read-only history pager for the venue /history command."""
        name = self.config.name
        agent_id = self.agent_id
        page_size = 10
        if arg:
            try:
                page_size = int(str(arg).strip())
            except (TypeError, ValueError):
                page_size = 10
        page_size = max(1, min(100, page_size))
        total = len(self.history)
        page = ((total + page_size - 1) // page_size) if total else 1
        while True:
            text, page, pages = self._venue_history_page(page, page_size)
            self.ui.interrupt_info(name, agent_id, 'Interrupt History', text)
            raw = self.ui.interrupt_read('history> ')
            if raw is None:
                return
            key = str(raw).strip()
            if key == '':
                key = 'n'
            low = key.lower()
            if low in ('q', 'quit', 'exit'):
                return
            if low in ('n', 'next'):
                if page >= pages:
                    self.ui.display_warning(name, agent_id, 'Already at the newest page.')
                else:
                    page += 1
                continue
            if low in ('p', 'prev', 'previous'):
                if page <= 1:
                    self.ui.display_warning(name, agent_id, 'Already at the oldest page.')
                else:
                    page -= 1
                continue
            parts = key.split(None, 1)
            verb = parts[0].lower()
            operand = parts[1].strip() if len(parts) > 1 else ''
            if verb == 'g':
                try:
                    target = int(operand)
                except (TypeError, ValueError):
                    self.ui.display_warning(name, agent_id, 'Usage: g <page>')
                    continue
                if target < 1 or target > pages:
                    self.ui.display_warning(
                        name, agent_id, 'Page %d is out of range 1..%d.' % (target, pages))
                page = max(1, min(target, pages))
                continue
            if verb == 'd':
                try:
                    idx = int(operand)
                except (TypeError, ValueError):
                    self.ui.display_warning(name, agent_id, 'Usage: d <idx>')
                    continue
                if idx < 0 or idx >= len(self.history):
                    self.ui.display_warning(name, agent_id, 'No message at index %d.' % idx)
                    continue
                msg = self.history[idx]
                role = str(msg.get('role') or '?')
                self.ui.interrupt_info(
                    name, agent_id, 'Interrupt History [%d] %s' % (idx, role),
                    str(msg.get('content') or ''))
                continue
            self.ui.display_warning(
                name, agent_id,
                "Unknown history key '%s'. Use n, p, g <page>, d <idx>, or q." % key)

    def _venue_memory_report(self):
        """The memory-index view for the venue /memory command."""
        try:
            entries = self._memory_index_entries()
        except Exception:
            entries = []
        session = getattr(self, 'session', None)
        workspace = str(getattr(session, 'workspace_dir', '') or '').strip()
        if workspace:
            folder = os.path.join(workspace, '.yaha', 'memory')
        else:
            folder = os.path.join('.yaha', 'memory')
        lines = ['memory folder: ' + folder]
        if entries:
            lines.append('files: %d' % len(entries))
            lines.extend(entries)
        else:
            lines.append('files: 0 (the memory folder is empty or missing)')
        return chr(10).join(lines)

    def _venue_chain_report(self, stage):
        """The prompt-chain view for the venue /chain command."""
        chain = list(getattr(self.config, 'prompt_chain', []) or [])
        idx = getattr(self, '_chain_index', 0) or 0
        detached = sorted((getattr(self.config, 'detached_stages', {}) or {}).keys())
        pos = (idx + 1) if chain else 0
        lines = ['chain: %d stage(s)  position %d/%d' % (len(chain), pos, len(chain))]
        for j, s in enumerate(chain):
            marker = '  <-' if j == idx else ''
            lines.append('  %d. %s%s' % (j + 1, s.get('name', 'Stage'), marker))
        lines.append('detached: ' + (', '.join(detached) if detached else 'none'))
        return chr(10).join(lines)

    def _venue_compress(self, stage):
        """Force an immediate context compression and report the outcome."""
        name = self.config.name
        agent_id = self.agent_id
        before = int(self._context_usage_ratio() * self.context_size)
        before_count = self.compress_count
        stage_prompt = getattr(self, '_current_stage_prompt', '') or ''
        try:
            rebuilt, _ = self._mid_loop_protection(
                stage or {}, stage_prompt, self._current_turn, force=True)
        except Exception as exc:
            self.ui.display_warning(name, agent_id, 'compression failed: %s' % exc)
            return
        after = int(self._context_usage_ratio() * self.context_size)
        report = ('compress: ~%d -> ~%d tokens (est.), delta ~%d, rebuilt=%s, count %d -> %d'
                  % (before, after, before - after, bool(rebuilt),
                     before_count, self.compress_count))
        self.ui.interrupt_info(name, agent_id, 'Interrupt Compress', report)

    def _venue_spawn(self, arg):
        """Spawn a sub-agent from the venue and show its output."""
        agent_class, _, task_text = str(arg).partition(' ')
        digest = self._run_spawn_interrupt(agent_class, task_text)
        if not digest:
            self.ui.display_warning(
                self.config.name, self.agent_id, 'Spawn produced no output.'
            )
            return
        self.ui.interrupt_answer(self.config.name, self.agent_id, digest)

    def _serve_interrupt_venue(self, stage, stage_turn, tools_called, entry='loop'):
        """Run the interrupt diagnose venue until a flow command is chosen.

        The venue is a REPL: a line WITHOUT a leading slash is a question to the
        model and never changes the chain; only a slash command changes the
        execution flow. Returns a VenueResult.
        """
        if not sys.stdin.isatty():
            # Non-TTY / unattended: never block and never call the model.
            return VenueResult(ic.STATUS_RESUME, None, None)
        self._serving_interrupt = True
        self._venue_entry = entry
        self._interrupt_suspend()
        transcript = []
        name = self.config.name
        agent_id = self.agent_id
        stage_name = (stage or {}).get('name', 'Stage')
        try:
            self.ui.interrupt_banner(name, agent_id, ic.venue_banner(stage_name))
            while True:
                raw = self.ui.interrupt_read()
                if raw is None:
                    # EOF / Ctrl-D is an explicit act: leave by resuming.
                    return VenueResult(ic.STATUS_RESUME, None, None)
                line = str(raw).strip()
                if not line:
                    continue
                if not ic.is_command(line):
                    answer = self._venue_chat(line, transcript)
                    self.ui.interrupt_answer(name, agent_id, answer)
                    continue
                spec, arg = ic.parse_interrupt_line(line)
                if spec is None:
                    token = ic.command_token(line) or ''
                    self.ui.display_warning(
                        name, agent_id,
                        f"Unknown venue command '/{token}'. Type /help for the command list.",
                    )
                    continue
                if spec.arg == ic.ARG_REQUIRED and not arg:
                    self.ui.display_warning(name, agent_id, 'Usage: ' + ic.usage_of(spec))
                    continue
                if spec.arg == ic.ARG_NONE and arg:
                    self.ui.display_warning(name, agent_id, 'Usage: ' + ic.usage_of(spec))
                    continue
                if spec.name == 'help':
                    self.ui.interrupt_help(name, agent_id, ic.render_help())
                    continue
                if spec.name == 'status':
                    self.ui.interrupt_status(
                        name, agent_id,
                        self._venue_status_report(stage, stage_turn, tools_called),
                    )
                    continue
                if spec.name == 'call':
                    self._venue_call(arg)
                    continue
                if spec.name == 'spawn':
                    self._venue_spawn(arg)
                    continue
                if spec.name == 'history':
                    self._venue_history_browser(arg)
                    continue
                if spec.name == 'memory':
                    self.ui.interrupt_info(name, agent_id, 'Interrupt Memory',
                                           self._venue_memory_report())
                    continue
                if spec.name == 'chain':
                    self.ui.interrupt_info(name, agent_id, 'Interrupt Chain',
                                           self._venue_chain_report(stage))
                    continue
                if spec.name == 'compress':
                    self._venue_compress(stage)
                    continue

                if spec.name == 'resume':
                    self.ui.interrupt_note(
                        name, agent_id,
                        f"Resuming stage '{stage_name}' at turn {self._current_turn}.",
                    )
                    return VenueResult(ic.STATUS_RESUME, None, arg)
                if spec.name == 'jump':
                    return VenueResult(ic.STATUS_JUMP + ':' + str(arg), arg, None)
                if spec.name == 'quit':
                    return VenueResult(ic.STATUS_FAIL + ':' + self._quit_target(stage), arg, None)
                return VenueResult(spec.status, arg, None)
        finally:
            self._serving_interrupt = False
            self._interrupt_resume()

    def _maybe_user_interrupt(self, stage, stage_turn, tools_called):
        """Serve a pending interrupt and return a VenueResult, or None.

        The pending flag lives on the engine-shared controller, so ANY agent
        on the runtime (root or peer) services the Esc that lands while it
        holds the CPU, against its own stage context.
        """
        ctl = self.stdin_interrupt
        if ctl is None or not ctl.is_pending():
            return None
        if self._serving_interrupt or self._compressing:
            return None
        ctl.clear_pending()
        return self._serve_interrupt_venue(stage, stage_turn, tools_called, entry='loop')

    def _maybe_boundary_venue(self, stage):
        """Stage-boundary poll: drain a pending Esc before leaving a stage.

        The loop-top poll cannot fire here (the stage loop already returned), so
        without this a pending interrupt would be serviced against the NEXT
        stage's context.
        """
        ctl = self.stdin_interrupt
        if ctl is None or not ctl.is_pending():
            return None
        if self._serving_interrupt or self._compressing:
            return None
        ctl.clear_pending()
        return self._serve_interrupt_venue(
            stage,
            self._last_stage_turn,
            self._last_stage_tools_called,
            entry='loop',
        )

    def _consume_deferred_interrupt_status(self, turn):
        """Return a chain-move tuple if an interrupt deferred one, else None.

        Used by the ReAct tool branches so a chain move requested from inside
        ask_user (rewind / jump / restart / quit) is honoured no matter which
        branch executed the tool. The flag is one-shot and cleared on
        consumption. A synthetic stand-in message replaces the tool result, so
        the assistant tool-call never dangles without a result in history.
        """
        deferred = self._pending_interrupt_status
        if not deferred:
            return None
        self._pending_interrupt_status = None
        label = ic.label_for_status(deferred) or deferred
        if deferred == ic.STATUS_REWIND:
            note = "Interrupt: rewind requested; re-entering the current stage."
        elif deferred.startswith(ic.STATUS_JUMP):
            note = "Interrupt: jump requested."
        elif deferred == ic.STATUS_RESTART:
            note = "Interrupt: restart requested."
        else:
            note = "Interrupt: quit requested; routing to the failure target."
        # Record the tool outcome so the model (and the log) can see why the
        # stage ended, not just that it did.
        self.history.append({"role": "user", "content": note})
        self.logger.log_turn(turn, self.history)
        # Match the loop-top interrupt path, which does not advance the turn.
        return (f"Interrupt: {label} requested.", deferred, turn)

    def _run_react_loop(self, start_turn, stage, stage_prompt=""):
        """Runs the ReAct turn loop for a single prompt stage."""
        turn = start_turn
        stage_name = stage.get("name", "Stage")
        # A new stage starts with a clean high-water mark, so the Snap gate measures
        # THIS stage's peak. The guard keeps a detached handler (which runs through
        # this same loop while _compressing is set) from wiping the outer stage's peak.
        if not self._compressing:
            self._stage_peak_ratio = 0.0
            # Remember the stage prompt so a venue /compress can re-inject it.
            self._current_stage_prompt = stage_prompt
        required_tools = set(stage.get("required_tools", []) or [])
        exit_tools = set(stage.get("exit_tools", []) or [])
        allowed_tools = set(stage.get("allowed_tools", []) or [])
        # One definition of the failure target (see _stage_failure_target), so a
        # stage without `Fail Target` cannot default differently per path.
        fail_target = self._stage_failure_target(stage)
        tools_called = set()
        stage_turn = 0
        claim_bounces = 0

        # If allowed_tools is specified, ensure required and exit tools are callable.
        effective_allowed = None
        if allowed_tools:
            effective_allowed = allowed_tools | required_tools | exit_tools

        while True:
            # Publish the venue context (read by /status and the boundary poll)
            # before any interrupt can be serviced.
            self._current_turn = turn
            self._last_stage_turn = stage_turn
            self._last_stage_tools_called = set(tools_called)

            # User interrupt (Esc) is serviced before a new turn is counted.
            interrupt_decision = self._maybe_user_interrupt(stage, stage_turn, tools_called)
            if interrupt_decision is not None:
                steer = interrupt_decision.text
                if steer:
                    # /resume <text>: one separate operator message appended
                    # right after the tool result. The tool observation itself
                    # stays byte-identical.
                    self.history.append({
                        "role": "user",
                        "content": f"[USER INTERRUPT: {steer}]",
                    })
                if interrupt_decision.status == ic.STATUS_RESUME:
                    if steer:
                        self.logger.log_turn(turn, self.history)
                    continue
                self.logger.log_turn(turn, self.history)
                if interrupt_decision.status == ic.STATUS_REWIND:
                    return "Interrupt: rewind requested.", ic.STATUS_REWIND, turn
                if interrupt_decision.status.startswith(ic.STATUS_FAIL):
                    return "Interrupt: quit requested.", interrupt_decision.status, turn
                if interrupt_decision.status == ic.STATUS_RESTART:
                    return "Interrupt: restart requested.", ic.STATUS_RESTART, turn
                return "Interrupt: jump requested.", interrupt_decision.status, turn

            stage_turn += 1

            # High-water safety net: never let a long stage overrun the window.
            # Formal stage-end Snap is separate (`_maybe_snap` from Agent.run);
            # this mid-loop check is emergency protection and may use the same
            # detached handler.
            if not self._compressing:
                restarted, turn = self._mid_loop_protection(stage, stage_prompt, turn)
                if restarted:
                    tools_called = set()
                    stage_turn = 0

            # Deliver every background (async_bash) result that finished
            # since the last turn, so the model sees it on the message it is
            # about to receive instead of having to poll.
            self._drain_async_results()

            # Call LLM; a full round of failures asks the user to wait or break.
            retry_count = 0
            unattended_round_count = 0
            last_failure_hint = "API call returned an empty response"
            while True:
                with self.ui.spinner(f"Agent {self.config.name} thinking (Turn {turn})..."):
                    response, usage, grounding_metadata = self.llm.generate(self.history, context_size=self.context_size)
                
                if usage and usage.get("prompt_tokens"):
                    self._record_usage(usage)
                
                # API-level failure: provider returned an error string / no usable text.
                api_error = (
                    not isinstance(response, str)
                    or not response.strip()
                    or response.startswith("Error calling "))
                if api_error:
                    last_failure_hint = str(response or "API call returned an empty response").strip()
                    # Silent recovery log: this failure is absorbed by a retry.
                    self.log_error(
                        "api",
                        last_failure_hint,
                        stage=stage_name,
                        turn=turn,
                        attempt=retry_count + 1,
                    )
                else:
                    if hasattr(self, 'track_tokens'):
                        self.track_tokens(self.config.model, usage)
                    
                    # One-pass optimization: no tools -> plain text is the final answer.
                    if not self.config.toolset:
                        validation_error = self._observation_error(response)
                        if validation_error is None:
                            self.history.append({"role": "assistant", "content": response})
                            self.ui.display_final_answer(self.config.name, self.agent_id, response)
                            return response, "DONE", turn + 1
                    else:
                        tool_call, validation_error = self.validate_response(response)
                        if not validation_error:
                            break
                    last_failure_hint = str(validation_error).strip()
                    # Silent recovery log: a malformed response is retried too.
                    self.log_error(
                        "api.validation",
                        last_failure_hint,
                        stage=stage_name,
                        turn=turn,
                        attempt=retry_count + 1,
                    )
                    # Feed the malformed response and the error back into history
                    # so the model can self-correct. Retrying with identical
                    # history makes near-deterministic models (e.g. DeepSeek
                    # leaking DSML markup) repeat the same malformation forever.
                    self.history.append({"role": "assistant", "content": response})
                    self.history.append({"role": "user", "content": f"Error: {validation_error}"})
                
                retry_count += 1
                if retry_count >= self.llm_retry_limit:
                    action = self._prompt_retry_wait_or_break(stage_name, last_failure_hint)
                    if action == "break":
                        error_msg = f"Error: LLM API retries exhausted after {self.llm_retry_limit} attempts and user chose to break."
                        self.history.append({"role": "user", "content": error_msg})
                        self.ui.display_error(self.config.name, self.agent_id, error_msg)
                        return error_msg, "ERROR", turn
                    if action == "wait":
                        # Explicit user wait keeps interactive retries unbounded and
                        # resets the unattended counter.
                        unattended_round_count = 0
                        self.ui.display_warning(
                            self.config.name,
                            self.agent_id,
                            f"Waiting {self.api_retry_wait_seconds:.0f}s before another retry round...",
                        )
                        time.sleep(self.api_retry_wait_seconds)
                        retry_count = 0
                        continue
                    # action None (no reply / absent user).
                    if not self._retry_is_unattended():
                        # Attended TTY run: no automatic limited-round abort. The
                        # operator may still break by typing break / Ctrl+C at any
                        # future prompt.
                        self.ui.display_warning(
                            self.config.name,
                            self.agent_id,
                            "No reply yet; attended run keeps retrying until you break or press Ctrl+C.",
                        )
                        retry_count = 0
                        continue
                    # Genuinely unattended: the prompt timeout is the cooldown, so
                    # start another round without extra wait, but cap the number
                    # of no-reply rounds to avoid an endless hidden loop.
                    unattended_round_count += 1
                    cap = self.api_retry_unattended_max_rounds
                    self.ui.display_warning(
                        self.config.name,
                        self.agent_id,
                        f"No user response; unattended retry round {unattended_round_count}/{cap}.",
                    )
                    if cap > 0 and unattended_round_count >= cap:
                        error_msg = f"Error: No user response after {unattended_round_count} unattended retry rounds. Aborting."
                        self.history.append({"role": "user", "content": error_msg})
                        self.ui.display_error(self.config.name, self.agent_id, error_msg)
                        return error_msg, "ERROR", turn
                    retry_count = 0
                    continue
                
                wait_time = min(2 ** (retry_count + 1), 30)
                self.ui.display_warning(
                    self.config.name,
                    self.agent_id,
                    f"{last_failure_hint[:200]} Retrying in {wait_time:.1f}s...",
                )
                time.sleep(wait_time)

            self.history.append({"role": "assistant", "content": response})
            
            if not tool_call:
                if exit_tools:
                    # A plain text response is not allowed when exit tools are required.
                    error_msg = f"Error: Stage '{stage_name}' requires an exit tool ({', '.join(sorted(exit_tools))}) to complete. You have just output a pure text response. You can continue work or call one of them to end."
                    self.history.append({"role": "user", "content": error_msg})
                    self.ui.display_error(self.config.name, self.agent_id, error_msg)
                    turn += 1
                    continue

                if self._has_real_observation():
                    # A real observation resets the counter: only CONSECUTIVE
                    # ungrounded claims are bounded.
                    claim_bounces = 0
                observation_error = self._observation_error(response, check_claims=True)
                if observation_error is not None:
                    error_msg = 'Error: ' + observation_error
                    self.history.append({"role": "user", "content": error_msg})
                    self.ui.display_error(self.config.name, self.agent_id, error_msg)
                    claim_bounces += 1
                    if claim_bounces >= self.llm_retry_limit:
                        error_msg = ('Error: the model kept claiming a verified outcome '
                                     'without any observation after %d attempts.' % claim_bounces)
                        self.history.append({"role": "user", "content": error_msg})
                        self.ui.display_error(self.config.name, self.agent_id, error_msg)
                        return error_msg, "ERROR", turn
                    turn += 1
                    continue

                # Final answer for this stage
                display_content = response
                if not self.verbose:
                    display_content = re.sub(r'<reasoning>.*?</reasoning>\s*', '', display_content, flags=re.DOTALL).strip()
                
                self.ui.display_final_answer(self.config.name, self.agent_id, display_content)
                self.logger.log_turn(turn, self.history)
                return response, "DONE", turn + 1
            
            # Display thought or public preceding text
            # Find the starting index of any potential tool code block (including alternative DSML patterns)
            start_patterns = [r"<tool_code>", r"<[|｜]{2,}DSML[|｜]{2,}tool_code>", r"<[|｜]{2,}tool_code>",
                              r"<[|｜]{2,}DSML[|｜]{2,}\s*calls>", r"<[|｜]{2,}DSML[|｜]{2,}\s*invoke\b"]
            tool_start_idx = len(response)
            for p in start_patterns:
                m = re.search(p, response, re.IGNORECASE)
                if m:
                    tool_start_idx = min(tool_start_idx, m.start())
            
            # The preceding text is everything before the first matched tool block
            thought = response[:tool_start_idx].strip()
            if thought:
                # Any text outside of <reasoning> blocks is public content intended for the user.
                # If there is non-empty public content preceding any tool call, we must display it!
                public_content = thought
                if "<reasoning>" in thought:
                    if "</reasoning>" in thought:
                        public_content = re.sub(r'<reasoning>.*?</reasoning>\s*', '', thought, flags=re.DOTALL).strip()
                    else:
                        # Handle unclosed/open-ended reasoning block: treat everything after the tag as reasoning
                        public_content = thought.split("<reasoning>", 1)[0].strip()
                
                if public_content:
                    self.ui.display_progress(self.config.name, self.agent_id, public_content)
                
                # In verbose mode, display the full raw thought (including reasoning)
                if self.verbose:
                    self.ui.display_thought(self.config.name, self.agent_id, thought)
            
            # Execute tool
            tool_name = tool_call.get("tool")
            args = tool_call.get("args", {})
            # Two accepted tool-call shapes: {"tool": n, "args": {...}} and the
            # inlined {"tool": n, "arg1": v, ...}. Only fall back to the inlined
            # shape when there is no args key at all, so an EXPLICIT empty args
            # dict calls the tool with no kwargs instead of being rewritten into
            # a stray args={} keyword (which surfaced as an unhelpful TypeError).
            if not args and "args" not in tool_call:
                 args = {k: v for k, v in tool_call.items() if k != "tool"}

            # A non-object args payload (a JSON array, a bare string, ...) is a
            # malformed call: every downstream consumer (the UI summary, the
            # hook kernels, the tool impl) expects a dict, and an AttributeError
            # there would kill the whole run. Feed it back so the model can
            # re-emit the call in the documented shape.
            if not isinstance(args, dict):
                error_result = ("Error: Malformed tool call: 'args' must be a JSON "
                                "object mapping parameter names to values, got %s. "
                                "Re-emit the call as {\"tool\": \"%s\", \"args\": {...}}."
                                % (type(args).__name__, tool_name))
                self.ui.display_error(self.config.name, self.agent_id, error_result)
                self.history.append({"role": "user", "content": error_result})
                turn += 1
                continue
            
            # Strict authorization check (framework exit tools are always allowed)
            framework_tools = {"restart", "loop_tool", "success_tool", "fail_tool",
                               "set_event_success", "set_event_jump",
                               "set_event_loop", "exit_to"}
            if tool_name not in framework_tools and tool_name not in self.config.toolset:
                tool_result = f"Error: Tool '{tool_name}' is not authorized."
            elif effective_allowed is not None and tool_name not in effective_allowed:
                tool_result = f"Error: Tool '{tool_name}' is not allowed in stage '{stage_name}'. Allowed tools: {', '.join(sorted(effective_allowed))}."
            elif tool_name not in self.tool_map:
                tool_result = f"Error: Tool '{tool_name}' not found."
            elif tool_name == "fail_tool":
                # Explicit stage failure. Does not require required_tools to be met.
                if exit_tools and tool_name not in exit_tools:
                    error_result = f"Error: Tool '{tool_name}' is not an authorized exit tool for stage '{stage_name}'. Authorized exit tools: {', '.join(sorted(exit_tools))}."
                    self.ui.display_error(self.config.name, self.agent_id, error_result)
                    self.history.append({"role": "user", "content": error_result})
                    turn += 1
                    continue

                self.ui.display_tool_call(self.config.name, self.agent_id, tool_name, args)
                tool_result, hook_failure, hook_ran = self._execute_tool_with_hooks(
                    stage, tool_name, args, stage_turn, turn, fail_target,
                    exits_stage=True)
                if hook_failure is not None:
                    return self._hook_stage_failure(stage, hook_failure, turn, fail_target)

                if hook_ran:
                    tools_called.add(tool_name)
                final_response = args.get("reason", "")
                self.history.append({"role": "user", "content": f"Stage '{stage_name}' failed. Routing to '{fail_target}' for failure handling."})
                if final_response:
                    self.history.append({"role": "assistant", "content": final_response})
                self.logger.log_turn(turn, self.history)
                return final_response or response, f"FAIL:{fail_target}", turn + 1
            else:
                is_exit = tool_name in exit_tools
                is_control = tool_name in {"loop_tool", "restart",
                                           "set_event_success", "set_event_jump",
                                           "set_event_loop", "exit_to"}

                if is_exit or is_control:
                    # Control/exit tools may only terminate the stage if required tools were called.
                    missing = required_tools - tools_called
                    if missing:
                        error_result = f"Error: Stage '{stage_name}' cannot complete yet. Required tools not called: {', '.join(sorted(missing))}. Please call them before using {tool_name}."
                        self.ui.display_error(self.config.name, self.agent_id, error_result)
                        self.history.append({"role": "user", "content": error_result})
                        turn += 1
                        continue

                    # When exit_tools is explicit, only those tools (plus the
                    # event-signalling framework terminators) may end the stage.
                    # NOTE: the event terminators are exempt from the Exit Tools
                    # list but - like every tool - remain subject to a stage's
                    # Allowed Tools list (the effective_allowed gate above) and
                    # to the agent Toolset for advertising.
                    if (exit_tools and tool_name not in exit_tools
                            and tool_name not in {"set_event_success",
                                                  "set_event_jump",
                                                  "set_event_loop",
                                                  "exit_to"}):
                        error_result = f"Error: Tool '{tool_name}' is not an authorized exit tool for stage '{stage_name}'. Authorized exit tools: {', '.join(sorted(exit_tools))}."
                        self.ui.display_error(self.config.name, self.agent_id, error_result)
                        self.history.append({"role": "user", "content": error_result})
                        turn += 1
                        continue

                    # Pre-validate a chain-move target BEFORE the tool runs:
                    # an unknown target, or one naming the CURRENT stage,
                    # keeps the stage alive and must NOT let set_event_jump
                    # fire its events for a move that never happens. (A
                    # self-move is a loop: loop_tool / set_event_loop do that.)
                    jump_resolved = None
                    if tool_name in ("exit_to", "set_event_jump"):
                        key = "name" if tool_name == "exit_to" else "target"
                        target_name = str(args.get(key) or "").strip()
                        jump_resolved = self._resolve_exit_to(target_name)
                        if jump_resolved is None:
                            error_result = (
                                "Error: %s target '%s' is neither a stage of "
                                "this class's chain nor a declared Detached "
                                "stage." % (tool_name, target_name))
                            self.ui.display_error(self.config.name, self.agent_id, error_result)
                            self.history.append({"role": "user", "content": error_result})
                            turn += 1
                            continue
                        # Only a positional JUMP to the current stage is
                        # refused (that is a loop: loop_tool / set_event_loop
                        # do that). A CALL naming the current stage is plain
                        # recursion: allowed, and still capped by the call-depth
                        # guard in ChainWalk.push_call.
                        if (jump_resolved[0] == "stage"
                                and jump_resolved[1].strip().lower() == stage_name.strip().lower()):
                            error_result = (
                                "Error: %s cannot target the CURRENT stage "
                                "('%s'); call loop_tool (or set_event_loop) to "
                                "re-enter it." % (tool_name, jump_resolved[1]))
                            self.ui.display_error(self.config.name, self.agent_id, error_result)
                            self.history.append({"role": "user", "content": error_result})
                            turn += 1
                            continue

                    self.ui.display_tool_call(self.config.name, self.agent_id, tool_name, args)
                    tool_result, hook_failure, hook_ran = self._execute_tool_with_hooks(
                        stage, tool_name, args, stage_turn, turn, fail_target,
                        exits_stage=True)
                    if hook_failure is not None:
                        return self._hook_stage_failure(stage, hook_failure, turn, fail_target)

                    # An UNBOUND placeholder can never be satisfied, so it is a
                    # STAGE FAILURE (routed through the stage's Fail Target), not
                    # a retryable typo: the stage must not stay alive waiting for
                    # a handshake that cannot happen. `_resolve_event_name` marks
                    # exactly this case, and only this case.
                    if (tool_name in ("set_event_success", "set_event_jump",
                                      "set_event_loop")
                            and str(tool_result).startswith(
                                self._UNRESOLVED_PLACEHOLDER)):
                        message = str(tool_result)
                        self.history.append({"role": "user", "content": message})
                        self.ui.display_error(self.config.name, self.agent_id, message)
                        self.logger.log_turn(turn, self.history)
                        return message, f"FAIL:{fail_target}", turn + 1

                    # An event terminator that refused to run (e.g. a call with
                    # no event name at all) must NOT silently complete the
                    # stage: report it and keep the stage alive. It is not
                    # counted as called.
                    if (tool_name in ("set_event_success", "set_event_jump",
                                      "set_event_loop")
                            and str(tool_result).startswith("Error")):
                        error_result = str(tool_result)
                        self.ui.display_error(self.config.name, self.agent_id, error_result)
                        self.history.append({"role": "user", "content": error_result})
                        turn += 1
                        continue

                    if hook_ran:
                        tools_called.add(tool_name)

                    # A tool may have deferred an interrupt-driven chain move
                    # (e.g. ask_user listed as an exit tool in a stage).
                    consumed = self._consume_deferred_interrupt_status(turn)
                    if consumed is not None:
                        return consumed

                    if jump_resolved is not None:
                        # exit_to / set_event_jump: the target was validated
                        # BEFORE the tool ran, so the move cannot vanish here.
                        final_response = str(args.get("summary") or "").strip() or response
                        self.history.append({"role": "user", "content": f"Stage '{stage_name}' moves to '{jump_resolved[1]}' via {tool_name}."})
                        if str(args.get("summary") or "").strip():
                            self.history.append({"role": "assistant", "content": str(args.get("summary")).strip()})
                        self.logger.log_turn(turn, self.history)
                        return (final_response, "EXIT_TO:" + jump_resolved[1], turn + 1)
                    if tool_result == "RESTART_SIGNALED":
                        self.logger.log_turn(turn, self.history)
                        return "Chain restart requested by agent.", "RESTART", turn + 1
                    elif tool_result == "LOOP_SIGNALED":
                        # loop_tool / set_event_loop: re-enter THIS stage. The
                        # summary is the stage's result, exactly as in
                        # success_tool; the modifier rides the next entry.
                        final_response = str(args.get("summary") or "").strip() or response
                        self.history.append({"role": "user", "content": f"Stage '{stage_name}' loops back to itself via {tool_name}."})
                        if str(args.get("summary") or "").strip():
                            self.history.append({"role": "assistant", "content": str(args.get("summary")).strip()})
                        self.logger.log_turn(turn, self.history)
                        return final_response, "LOOP", turn + 1

                    if tool_name == "set_event_success":
                        # `summary` is the stage's RESULT: it becomes the
                        # stage's final response; nothing is injected later.
                        final_response = args.get("summary", "")
                        self.history.append({"role": "user", "content": f"Stage '{stage_name}' completed successfully via set_event_success."})
                        if final_response:
                            self.history.append({"role": "assistant", "content": final_response})
                        self.logger.log_turn(turn, self.history)
                        return final_response or response, "DONE", turn + 1

                    if tool_name == "success_tool":
                        final_response = args.get("summary", "")
                        self.history.append({"role": "user", "content": f"Stage '{stage_name}' completed successfully via success_tool."})
                        if final_response:
                            self.history.append({"role": "assistant", "content": final_response})
                        self.logger.log_turn(turn, self.history)
                        return final_response or response, "DONE", turn + 1
                else:
                    self.ui.display_tool_call(self.config.name, self.agent_id, tool_name, args)
                    tool_result, hook_failure, hook_ran = self._execute_tool_with_hooks(
                        stage, tool_name, args, stage_turn, turn, fail_target,
                        exits_stage=False)
                    if hook_failure is not None:
                        return self._hook_stage_failure(stage, hook_failure, turn, fail_target)

                    if hook_ran:
                        tools_called.add(tool_name)

                    # A tool (notably ask_user) may have deferred a chain move
                    # requested through the user-interrupt scratchpad. Consume
                    # the one-shot flag and move the chain here, where the outer
                    # stage loop knows how to route it.
                    consumed = self._consume_deferred_interrupt_status(turn)
                    if consumed is not None:
                        return consumed

            # Determine Status and clean output
            status = "SUCCESS"
            clean_result = tool_result
            if tool_result.startswith("Error"):
                status = "ERROR"
                self.ui.display_error(self.config.name, self.agent_id, tool_result)
            else:
                self.ui.display_tool_result(self.config.name, self.agent_id, tool_name, tool_result)
            
            # Track the last tool result for the venue /status ground truth.
            self._last_tool_result = (tool_name, status)

            # Build Structured result_msg
            size_kb = len(tool_result.encode('utf-8')) / 1024
            result_msg = f"TOOL RESULT ({tool_name}):\n"
            result_msg += f"- **Status**: {status}\n"
            result_msg += f"- **Output Size**: {size_kb:.1f}KB\n"
            
            # Handle nested code blocks
            delimiter = "```"
            if "```" in clean_result:
                delimiter = "~~~"
                
            result_msg += f"- **Content**:\n{delimiter}\n{clean_result}\n{delimiter}"

            self.history.append({"role": "user", "content": result_msg})
            # Log turn to persistent log
            self.logger.log_turn(turn, self.history)
            turn += 1

        return "Error: Turn limit reached.", "ERROR", turn

    # --- Context auto-compression -------------------------------------------

    def _hard_force_ratio(self):
        """The strict emergency watermark for THIS model's window.

        The net must be able to complete its own compression: when it fires, it runs the
        Snap handler (or the reflection prompt) over a near-full history, so the request
        carries a prompt at the bar PLUS up to `max_tokens` completions. A bare constant
        cannot know that, which is why the shipped 128K models (max_tokens 8192) used to
        leave only 6400 tokens of headroom - less than one maximum request - and the
        handler would be rejected for exceeding the window, leaving the net unable to do
        its job exactly when it was needed.

        The bar therefore leaves `max_tokens + HARD_FORCE_OUTPUT_MARGIN` free and uses
        HARD_FORCE_RATIO as a CAP, so large windows keep the documented 0.95 while a
        smaller window gets a workable, lower bar. Windows too small for the invariant at
        all sit on HARD_FORCE_FLOOR.
        """
        total = self.context_size or 0
        if not total:
            return HARD_FORCE_RATIO
        cfg = getattr(self, "model_cfg", None)
        max_tokens = getattr(cfg, "max_tokens", None) or HARD_FORCE_DEFAULT_MAX_TOKENS
        bar = 1.0 - (max_tokens + HARD_FORCE_OUTPUT_MARGIN) / total
        return min(HARD_FORCE_RATIO, max(HARD_FORCE_FLOOR, bar))

    def _context_usage_ratio(self):
        """Occupancy ratio of the context window, on ONE instrument.

        Grounded on the last provider-reported prompt token count plus the content
        accumulated since that report, converted with the calibrated
        characters-to-tokens relation. Before the first report there is no anchor and
        the calibrated cold-start prediction is used instead. A characters-derived
        estimate is NEVER written back into `_last_prompt_tokens`: that field belongs
        to the provider alone.
        """
        total = self.context_size or 0
        if not total:
            return 0.0
        return max(0.0, self._predicted_tokens() / total)

    def _history_chars(self):
        """Characters of model-visible history content (the estimate's only input)."""
        return sum(len(m.get("content") or "") for m in self.history)

    def _get_calibration(self):
        """The characters-to-tokens calibration, created on first use.

        Lazily created rather than assumed: an agent built without __init__ (the test
        stubs do this) must still be able to report usage, and a class-level mutable
        would be shared between agents.
        """
        cal = getattr(self, "_calibration", None)
        if cal is None:
            cal = ContextCalibration()
            self._calibration = cal
        return cal

    def _predicted_tokens(self):
        """Best available occupancy in provider-token units.

        With an anchor: `anchor + (chars_now - chars_at_anchor) * rate`, i.e. the
        observed relation `tokens ~= a * chars + b` applied from the anchor point, so
        a compression that shrinks the history lowers the prediction immediately
        without pretending to be a measurement. Without an anchor: the calibrated
        cold-start prediction over the whole history.
        """
        chars_now = self._history_chars()
        anchor = self._last_prompt_tokens or 0
        if anchor:
            baseline = self._chars_at_last_usage
            if baseline is None:
                return anchor
            delta_chars = chars_now - baseline
            if not delta_chars:
                return anchor
            rate = self._get_calibration().tokens_per_char()
            anchored = anchor + delta_chars * rate
            # Floor at the density lower bound: `rate * chars` is a genuine lower bound on
            # the true prompt while the calibration keeps its intercept non-negative, so a
            # compression that removes most of the history cannot collapse the prediction
            # to 0 - a 0 would hand the tool-output budget the LARGEST budget it can
            # produce while content still exists (the reviewer reproduced ~960 -> ~0).
            floor = rate * chars_now
            return max(0, int(max(anchored, floor)))
        return self._get_calibration().predict(chars_now)

    def _usage_is_estimated(self):
        """True when the displayed occupancy is a prediction, not a measurement."""
        if not self._last_prompt_tokens:
            return True
        return bool(self._usage_is_predicted)

    def _l1_sufficient(self, ratio, target):
        """Did a deterministic L1 pass bring the window under `target`?

        One implementation for both callers: the emergency high-water net passes
        HARD_FORCE_RATIO, the legacy handler-less path passes Snap To. The target is
        always passed explicitly, so the two thresholds can never be conflated the way
        `self.config.snap_threshold or 0.8` conflated them.
        """
        if target is None:
            return True
        return ratio < target

    def _record_usage(self, usage, prompt_chars=None):
        """Record a provider measurement. The ONLY writer of the anchor field.

        Keep this the single entry point: the anchor must never be set from an
        estimate, and the character baseline must always be written together with the
        count it belongs to, or the delta prediction measures two different prompts.

        `prompt_chars` is the character count of the prompt that was actually measured
        and defaults to the current history. Calls that measure a different prompt
        (the interrupt venue measures a copy of the history plus its transcript) pass
        it explicitly so the anchor and its baseline stay consistent.

        Also feeds the calibration and clears the predicted flag: after a real report
        the gauge is showing a measurement again.
        """
        if not usage:
            return
        tokens = usage.get("prompt_tokens") or 0
        try:
            tokens = int(tokens)
        except (TypeError, ValueError):
            return
        if tokens <= 0:
            return
        chars = self._history_chars() if prompt_chars is None else int(prompt_chars)
        self._get_calibration().observe(chars, tokens)
        self._last_prompt_tokens = tokens
        self._chars_at_last_usage = chars
        self._usage_is_predicted = False

    def _tool_output_budget(self):
        """Byte cap for ONE tool result: half of the remaining context window.

        The occupancy comes from the same anchored instrument that drives
        auto-compression (`_context_usage_ratio`), and the tokens-to-bytes conversion uses
        the CALIBRATED density (`core/context_calibration.py`), so the documented
        half-window bound holds at the real rate instead of at a nominal 4 chars/token.
        The unit conventions and the degradation rules live in tools/output_guard.py; the
        budget is re-resolved on every tool call, so it always reflects the history the
        model has accumulated so far.
        """
        from ..tools.output_guard import compute_output_budget
        used = int(self._context_usage_ratio() * self.context_size)
        rate = self._get_calibration().tokens_per_char()
        # The guard wants characters per token; a degenerate rate falls back to its own
        # default (None is not a valid density).
        chars_per_token = (1.0 / rate) if rate and rate > 0 else None
        return compute_output_budget(self.context_size, used, chars_per_token=chars_per_token)

    def _ctx_display_text(self):
        """Zero-arg UI provider: context-window usage suffix for section bars.

        Reuses the same occupancy ratio that drives auto-compression so the
        on-screen gauge reflects reality, and marks a PREDICTION as such: after a
        compression the figure is no longer a provider measurement and must not be
        mistaken for one. A reading at or above the window is shown truthfully
        instead of being flattened to 99%.
        """
        total = self.context_size or 0
        if not total:
            return ""
        ratio = self._context_usage_ratio()
        used = int(ratio * total)
        pct = ratio * 100
        suffix = " (est.)" if self._usage_is_estimated() else ""
        return f"Ctx usage: {pct:g}% ({used // 1000}K/{total // 1000}K){suffix}"

    def _maybe_snap(self, stage, turn, force=False, phase="stage-end"):
        """Run context compression (opt-in stage Snap: enabled, or forced).

        Runs the class-level Snap Handler — a detached stage — through the
        normal stage engine when the context ratio is at/above the Snap
        Threshold. The handler's final plain-text digest replaces the earlier
        conversation so the next stage/new-task sees a compacted context.

        The gate reads the stage's PEAK occupancy, not only the current one, so a
        cheap L1 pass inside the stage cannot cancel the Snap this stage opted into.
        """
        self._last_snap_rebuilt = False
        if self._compressing:
            return turn
        stage_name = (stage or {}).get("name", "Stage")
        opted_in = (stage or {}).get("snap") == "enabled"
        if not force and not opted_in:
            return turn

        ratio = max(self._context_usage_ratio(), self._stage_peak_ratio)
        threshold = self.config.snap_threshold
        if threshold is None:
            # No Snap policy for this class: only the strict watermark may force a
            # compression here, and that arrives as force=True from the emergency net.
            if not force:
                return turn
        elif not force and ratio < threshold:
            return turn

        handler_name = self.config.snap_handler
        if not handler_name:
            self.ui.display_warning(self.config.name, self.agent_id,
                                    f"No Snap Handler configured; skipping snap after stage '{stage_name}'.")
            return turn
        handler = (self.config.detached_stages or {}).get(handler_name)
        if not handler:
            self.ui.display_warning(self.config.name, self.agent_id,
                                    f"Snap Handler '{handler_name}' not found in detached stages; skipping snap after stage '{stage_name}'.")
            return turn

        before_tokens = int(ratio * self.context_size)
        target = (self.config.snap_to or 0.4)
        snap_prompt = ((handler.get("content") or "").strip()
                     + f"\n\n[SNAP REQUEST] Context usage is {ratio * 100:.0f}% "
                     + f"(target after snap: ~{target * 100:.0f}%). "
                     + f"Stage '{stage_name}' just completed. Persist durable "
                     + "facts/memory if needed, then reply with a plain-text summary of "
                     + f"the conversation in FEWER THAN {COMPRESSION_SUMMARY_MAX_LINES} "
                     + "LINES (aim for well under), using sections: GOAL, KEY LEARNINGS, "
                     + "DESIGN & ARCHITECTURE DECISIONS (most important), PENDING, "
                     + "KEY FILES, ERRORS & LESSONS. The summary replaces the prior "
                     + "conversation; the most recent ReAct steps are kept after it "
                     + "(subject to the L1 truncation rule).")

        original_history = list(self.history)
        original_turn = turn
        self._compressing = True
        try:
            handler_stage = dict(handler)
            handler_stage["content"] = snap_prompt
            self.history.append({"role": "user", "content": snap_prompt})
            self.logger.log_turn(turn, self.history)
            digest, status, turn = self._run_react_loop(turn + 1, handler_stage, snap_prompt)
            if status != "DONE" or not digest or not digest.strip():
                # Handler failed/aborted: keep the original conversation intact
                # rather than losing it.
                self.history = original_history
                return original_turn

            # Cold restart: rebuild system prompt so the MEMORY INDEX is
            # refreshed with any files the handler wrote during reflection, then
            # append the most recent ReAct steps (the L1 rule still applies).
            steps = self._recent_react_steps(self._compression_keep_steps(),
                                             history=original_history)
            self._rebuild_compressed_history(digest, steps=steps)
            self._last_snap_rebuilt = True
            final_tokens = int(self._context_usage_ratio() * self.context_size)
            self.compress_count += 1
            # A compression produces a PREDICTION, never an anchor: mark it as such
            # instead of writing a characters-derived number into the provider's field.
            self._usage_is_predicted = True
            note = (f"Snap handler '{handler_name}' after stage '{stage_name}'. "
                    f"~{before_tokens} -> ~{final_tokens} tokens (est.).")
            self.logger.log_compression_restart(self.history, note)
            # SNAP MARKER: a compression must be VISIBLE and attributable - record
            # when it happened, in which stage, and which events that stage had
            # already signalled.
            self.logger.log_compression(self._mark_snap(stage_name, phase))
            self.ui.display_compression(self.config.name, self.agent_id,
                                        before_tokens, final_tokens, "SNAP", self.compress_count)

            # Snap To is a target the handler is ASKED to reach; it was never verified
            # before. Check it on the corrected instrument and say so when it is missed
            # instead of letting a banner imply success.
            target = self.config.snap_to
            reached = self._context_usage_ratio()
            if target is not None and reached > target:
                warning = (f"Snap handler '{handler_name}' left the context at "
                           f"{reached * 100:.1f}%, above the configured "
                           f"{target * 100:.0f}% target.")
                self.logger.log_compression(f"{note} {warning}")
                self.ui.display_warning(self.config.name, self.agent_id, warning)
            return turn
        finally:
            self._compressing = False

    def _mark_snap(self, stage_name, phase, history=True):
        """Record + surface a SNAP MARKER; returns the one-line marker text.

        A context snapshot REWRITES the stage, so it must be visible and
        attributable: it records WHEN it fired, the STAGE it hit and the phase
        (stage-end or mid-stage), and which multi-agent EVENTS that stage had
        already signalled. The text is returned for the caller to log; when
        `history` is True (the handler Snap path) it is also appended to the
        conversation. The L2 cold restart passes history=False so its documented
        [system, digest, stage prompt] shape is preserved.
        """
        try:
            signalled = sorted(self._stage_signalled_events)
        except Exception:
            signalled = []
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        self._snap_marker = {
            "when": when,
            "stage": str(stage_name),
            "phase": str(phase),
            "signalled": signalled,
        }
        text = ("[SNAP MARKER] %s snap at stage '%s' (%s); events already "
                "signalled this stage: %s"
                % (when, stage_name, phase,
                   ", ".join(signalled) if signalled else "none"))
        if history:
            try:
                self.history.append({"role": "user", "content": text})
            except Exception:
                pass
        return text

    def _mid_loop_protection(self, stage, stage_prompt, turn, force=False):
        """High-water safety net inside a long-running stage (not Snap boundary).

        This is the STRICT net: it fires at HARD_FORCE_RATIO and it fires regardless of
        the stage's Snap policy, because its job is to stop a long stage from
        overrunning the window. The opt-in stage Snap at `snap_threshold` is a different
        mechanism with a different threshold; conflating the two (the old
        `self.config.snap_threshold or 0.8`) is what made this net fire at the Snap bar
        on every turn.

        Uses the formal detached handler when the class config has one; otherwise falls
        back to the legacy `_compress_context` so non-snap-policy configs keep working.

        Returns (history_was_rebuilt, turn)."""
        ratio = self._context_usage_ratio()
        # Track the stage high-water mark BEFORE any threshold test, so a cheap L1 pass
        # cannot hide it from the stage-end Snap gate.
        if ratio > self._stage_peak_ratio:
            self._stage_peak_ratio = ratio
        # The STRICT net only acts at or above its watermark, and that watermark is DERIVED
        # from this model's window (see _hard_force_ratio) so the net can always fit its own
        # request. Below it, compressing is the opt-in stage Snap's business, not this net's.
        bar = self._hard_force_ratio()
        if self._compressing or (not force and ratio < bar):
            return False, turn
        before_tokens = int(ratio * self.context_size)

        handler_name = self.config.snap_handler
        handler = None
        if handler_name:
            handler = (self.config.detached_stages or {}).get(handler_name)
        if not handler:
            # No formal handler/policy: preserve legacy paths/tests.
            return self._compress_context(stage, stage_prompt, turn)

        # Deterministic L1 truncation first; if that is enough, log it and keep the
        # running stage untouched. The re-check uses the SAME instrument as the trigger
        # (the anchored prediction), so the two numbers are actually comparable -
        # switching units mid-decision is what made L1 look sufficient when it was not.
        truncated = self._truncate_old_tool_results()
        after_l1 = self._context_usage_ratio()
        if self._l1_sufficient(after_l1, bar):
            if truncated:
                self.compress_count += 1
                after_tokens = int(after_l1 * self.context_size)
                self._usage_is_predicted = True
                note = (f"L1 safety truncation: compressed {truncated} tool result(s). "
                        f"~{before_tokens} -> ~{after_tokens} tokens (est.).")
                self.logger.log_compression(note)
                self.ui.display_compression(self.config.name, self.agent_id,
                                            before_tokens, after_tokens, "L1", self.compress_count)
            return False, turn

        # The net is now actually going to compress, so say so. When the stage never opted
        # in this cold restart would otherwise be invisible, so it gets a visible warning
        # as well as a log line. (Emitted here, AFTER the L1 decision: warning the operator
        # before knowing whether L1 alone suffices told them compression was happening when
        # nothing but a cheap truncation was.)
        if (stage or {}).get("snap") != "enabled":
            warning = (f"Strict {bar * 100:.0f}% context watermark crossed in "
                       f"stage '{stage.get('name', 'Stage')}' at {ratio * 100:.1f}%; "
                       f"compressing even though the stage did not enable Snap.")
            self.logger.log_compression(warning)
            self.ui.display_warning(self.config.name, self.agent_id, warning)

        # L1 could not bring the window under the strict watermark. It may have freed
        # nothing at all (everything truncatable is already a stub), which used to
        # return silently and leave the run above the watermark with no visible
        # compression at all. Record it, then escalate to the formal handler.
        self.logger.log_compression(
            f"L1 insufficient: {truncated} tool result(s) truncated, still at "
            f"{after_l1 * 100:.1f}% against the strict {bar * 100:.0f}% "
            f"watermark; escalating to the Snap handler."
        )

        # Formal handler path: run it even when the stage says Snap: disabled
        # (emergency/forced because we are above the class high watermark).
        turn = self._maybe_snap(stage, turn, force=True, phase="mid-stage")
        rebuilt = self._last_snap_rebuilt
        if rebuilt:
            # The handler digest replaced the conversation; restore the current
            # stage prompt so the loop can continue mid-stage.
            self.history.append({"role": "user", "content": (
                stage_prompt +
                "\n\n[CONTEXT RESTORED] The context above is a compressed digest of earlier "
                "work. Continue this stage from where it left off."
            )})
            return True, turn
        return False, turn

    def _compression_keep_steps(self):
        """How many recent ReAct steps a compression keeps (n).

        An agent-wide parameter: the class-level AgentClass key
        `Compression Keep Steps` when authored, else COMPRESSION_KEEP_STEPS.
        """
        value = getattr(self.config, "compression_keep_steps", None)
        if value is None:
            return COMPRESSION_KEEP_STEPS
        try:
            value = int(value)
        except (TypeError, ValueError):
            return COMPRESSION_KEEP_STEPS
        return max(0, value)

    def _recent_react_steps(self, n, history=None):
        """The last `n` ReAct steps (tool_call-result pairs) of a history.

        A step ends at a `TOOL RESULT (...)` user message and starts at the nearest
        preceding assistant message that carried the call. This is structural, not
        content-parsing, so DSML / tool_code dialects behave identically. Returns the
        messages in chronological order.
        """
        if not n or n <= 0:
            return []
        hist = self.history if history is None else history
        ends = [i for i, m in enumerate(hist)
                if m.get("role") == "user"
                and (m.get("content") or "").startswith(_TOOL_RESULT_PREFIXES)]
        if not ends:
            return []
        steps = []
        lower = 0
        for end in ends[-n:]:
            start = end
            for j in range(end - 1, lower - 1, -1):
                if hist[j].get("role") == "assistant":
                    start = j
                    break
            steps.extend(hist[start:end + 1])
            lower = end + 1
        return steps

    def _cap_summary_lines(self, text, max_lines=COMPRESSION_SUMMARY_MAX_LINES):
        """Hard-cap a compression summary strictly below `max_lines` lines.

        The prompts ask for FEWER THAN `max_lines` lines, so the cap keeps at
        most `max_lines - 1` lines and appends a one-line truncation note.
        """
        lines = (text or "").splitlines()
        if len(lines) < max_lines:
            return text
        keep = max_lines - 1
        return ("\n".join(lines[:keep])
                + "\n[... summary truncated to %d lines ...]" % keep)

    def _rebuild_compressed_history(self, summary, steps=None):
        """Build a compressed context: system prompt + summary + last ReAct steps.

        The L1 truncation rule still applies to the retained steps, so an oversized
        TOOL RESULT inside the window is replaced by the standard placeholder here
        exactly as it would be in the live history.
        """
        if steps is None:
            steps = self._recent_react_steps(self._compression_keep_steps())
        self.history = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": (
                "CONTEXT DIGEST (the earlier conversation was compressed; full details "
                "remain in the log file and the memory folder):\n\n"
                + self._cap_summary_lines(summary).strip()
            )},
        ]
        for msg in steps:
            self.history.append(dict(msg))
        # The L1 rule still governs the retained steps.
        self._truncate_old_tool_results()
        return self.history

    def _truncate_old_tool_results(self, keep_last=6, head_chars=500):
        """L1: replace old tool result messages with a truncated placeholder,
        in place (history length is preserved for incremental logging)."""
        count = 0
        cutoff = max(0, len(self.history) - keep_last)
        for i in range(1, cutoff):  # never touch the system prompt
            msg = self.history[i]
            content = msg.get("content") or ""
            if msg.get("role") != "user" or not content.startswith("TOOL RESULT ("):
                continue
            if len(content) <= head_chars + 200:
                continue
            tool_match = re.match(r"TOOL RESULT \(([^)]+)\):", content)
            tool_name = tool_match.group(1) if tool_match else "unknown"
            orig_kb = len(content.encode("utf-8")) / 1024
            msg["content"] = (
                f"COMPRESSED TOOL RESULT ({tool_name}):\n"
                f"- **Status**: COMPRESSED (original {orig_kb:.1f}KB, see the log file for the full output)\n"
                f"- **Content Head**:\n```\n{content[:head_chars]}\n```\n"
                f"[... truncated by context compression ...]"
            )
            count += 1
        return count

    def _reflect_for_compression(self, stage, turn):
        """L2: let the current agent reflect over the FULL history (prefix cache
        hit) and produce a structured digest. Memory write tools are allowed so
        durable facts land in .yaha/memory/ before the context is dropped.
        Returns the digest text, or None on failure."""
        stage_name = stage.get("name", "Stage")
        reflection_prompt = (
            "CONTEXT COMPRESSION - REFLECTION\n"
            "The conversation context is nearly full and is about to be compressed. "
            f"You are currently in stage '{stage_name}' of your prompt chain.\n"
            f"The original task was:\n{self.current_task_prompt}\n\n"
            "Before replying, you MAY update your memory folder (.yaha/memory/) with any durable "
            "facts, decisions, or lessons worth keeping across the compression (use write_file/append_file/edit_file). "
            "Keep memory updates small and precise.\n\n"
            "Then produce a COMPACT summary of the ENTIRE conversation so far in FEWER THAN "
            f"{COMPRESSION_SUMMARY_MAX_LINES} LINES (aim for well under {COMPRESSION_SUMMARY_MAX_LINES}). "
            "It will replace the earlier conversation, so keep only what is needed to continue.\n"
            "Use EXACTLY these sections:\n"
            "GOAL: the project/task goal and what you are trying to accomplish.\n"
            "KEY LEARNINGS: the durable lessons from the work so far.\n"
            "DESIGN & ARCHITECTURE DECISIONS: the important design/architecture choices that "
            "have been made and why (MOST IMPORTANT - do not omit this section).\n"
            "PENDING: what remains to be done, including the immediate next step.\n"
            "KEY FILES: important files/paths touched or needed, with one-line notes.\n"
            "ERRORS & LESSONS: failures encountered and how they were resolved (if any).\n\n"
            "Your final plain-text reply (no tool call) is taken as the summary."
        )
        reflection_stage = {
            "name": "Compression",
            "content": reflection_prompt,
            "allowed_tools": ["read_file", "write_file", "append_file", "edit_file",
                              "list_files", "grep_files", "search_files"],
        }
        self.history.append({"role": "user", "content": reflection_prompt})
        self.logger.log_turn(turn, self.history)
        digest, status, turn = self._run_react_loop(turn + 1, reflection_stage, reflection_prompt)
        if status != "DONE" or not digest or not digest.strip():
            return None, turn
        return digest.strip(), turn

    def _compress_context(self, stage, stage_prompt, turn):
        """Two-level compression pipeline for classes WITHOUT a Snap handler.

        Run L1 (best-effort truncation), check the configured Snap To target, and only
        escalate to L2 (reflection + cold restart) if L1 alone is not enough. The
        sufficiency decision goes through the shared `_l1_sufficient` helper with the
        target passed explicitly, instead of a hard-coded 0.4. Compression is unlimited.

        Returns (history_was_rebuilt, turn)."""
        self._compressing = True
        try:
            before_tokens = int(self._context_usage_ratio() * self.context_size)
            target = self.config.snap_to if self.config.snap_to is not None else 0.4

            # L1: deterministic in-place truncation of old tool results
            compressed_n = self._truncate_old_tool_results()
            after_l1 = self._context_usage_ratio()
            after_l1_tokens = int(after_l1 * self.context_size)
            # The most recent ReAct steps survive the L2 cold restart (the L1 rule
            # still applies to them; see _rebuild_compressed_history). Captured
            # BEFORE the reflection loop, so the reflection's own tool results are
            # never mistaken for steps.
            steps = self._recent_react_steps(self._compression_keep_steps())

            if self._l1_sufficient(after_l1, target):
                self.compress_count += 1
                self._usage_is_predicted = True
                note = (f"L1 truncation: compressed {compressed_n} tool result(s). "
                        f"~{before_tokens} -> ~{after_l1_tokens} tokens (est.).")
                self.logger.log_compression(note)
                self.ui.display_compression(self.config.name, self.agent_id,
                                            before_tokens, after_l1_tokens, "L1", self.compress_count)
                return False, turn

            # L2: in-place reflection over the full history, then cold restart
            digest, turn = self._reflect_for_compression(stage, turn)
            if not digest:
                # Fallback: keep the L1-compressed history, no restart
                self.compress_count += 1
                self._usage_is_predicted = True
                note = (f"L1 truncation: compressed {compressed_n} tool result(s). "
                        f"L2 reflection failed; keeping L1-compressed history "
                        f"(~{before_tokens} -> ~{after_l1_tokens} tokens, est.).")
                self.logger.log_compression(note)
                self.ui.display_compression(self.config.name, self.agent_id,
                                            before_tokens, after_l1_tokens, "L1 (L2 failed)", self.compress_count)
                return False, turn

            # Cold restart: build the compressed context (system prompt + summary
            # + the most recent ReAct steps; the L1 rule still applies to them),
            # then re-inject this stage's prompt so the loop can continue.
            self._rebuild_compressed_history(digest, steps=steps)
            self.history.append({"role": "user", "content": (
                stage_prompt +
                "\n\n[CONTEXT RESTORED] The context above is a compressed digest of earlier "
                "work. Continue this stage from where it left off."
            )})
            final_tokens = int(self._context_usage_ratio() * self.context_size)
            self.compress_count += 1
            self._usage_is_predicted = True
            note = (f"L2 reflection + cold restart from stage '{stage.get('name', 'Stage')}'. "
                    f"~{before_tokens} -> ~{final_tokens} tokens (est.).")
            self.logger.log_compression_restart(self.history, note)
            # SNAP MARKER: the L1/L2 cold restart is a compression too, so mark it
            # with the same visible attribution as the handler Snap path.
            self.logger.log_compression(
                self._mark_snap(stage.get('name', 'Stage'), 'l2-cold-restart',
                                history=False))
            self.ui.display_compression(self.config.name, self.agent_id,
                                        before_tokens, final_tokens, "L2", self.compress_count)
            return True, turn
        finally:
            self._compressing = False
