# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
from .session import Session
from .agent import Agent
from .access import AccessController
from .md_loader import load_md_with_includes
from .soulstore import soul_for_class
from ..config.models import parse_model_md
from ..config.agents import load_agent_classes
from ..config.tools import load_tool_schema
from .runtime import AgentRuntime, RuntimeDeadlockError, validate_wait_events
from ..ui.terminal import reset_multi_agent_state
from ..ui.interrupt import StdinInterruptController

class YAHAEngine:
    def __init__(self, workspace_dir=".", include_agent_files=None, extra_skill_dirs=None, verbose=False, yes_for_all=False, unattended=None):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.yaha_dir = os.path.join(self.workspace_dir, ".yaha")
        self.session = Session(workspace_dir=self.workspace_dir, extra_skill_dirs=extra_skill_dirs)
        self.access_controller = AccessController(workspace_root=self.workspace_dir, yes_for_all=yes_for_all)
        self.token_usage = {} # model_name -> {"input": 0, "output": 0}
        self.verbose = verbose
        # Unattended mode for API retry control (None => CLI/tty decides later).
        self.unattended = unattended
        # Stdin / Esc-interrupt controller: one per engine, created lazily and
        # shared by every agent this engine makes. Installed only while a root
        # run is active; agents never own stdin themselves.
        self._stdin_interrupt = None
        
        # Load configs
        self.models_config = parse_model_md(os.path.join(self.yaha_dir, "Model.md"))
        
        # Primary Agent Classes, plus any EXTRA class files given with `-I`.
        # Each file is parsed as its OWN document and the class maps are merged
        # in order - the primary first, each extra after it, a later definition
        # winning a name clash WHOLESALE - and inheritance is resolved once over
        # the merged map. This is NOT `<!--include-->`, which is macro processing
        # that inlines TEXT into one document sharing one macro tree; macros also
        # stay per-file for `-I`.
        agent_class_file = os.path.join(self.yaha_dir, "AgentClass.md")
        include_files = list(include_agent_files or ())
        # ONE read of every document yields both the resolved classes and the
        # definition report the diagnostics below are built from - so a class
        # file (and its includes) is loaded and macro-expanded exactly once.
        self.agent_classes, self.class_sources = load_agent_classes(
            agent_class_file, include_files)
        self.class_conflicts = {
            name: entries for name, entries in self.class_sources.items()
            if sum(count for _path, count in entries) > 1
        }
        self.unknown_parents = [
            (name, config.inherited)
            for name, config in self.agent_classes.items()
            if config.inherited not in (None, "None")
            and config.inherited not in self.agent_classes
        ]
        

        
        self.tool_schemas = load_tool_schema(os.path.join(self.yaha_dir, "tool-schema.md"))
        
        # Load SOUL once, then hand each class its own identity section.
        # `soul_text` is the raw file; `soul_prompt` stays the DEFAULT identity
        # (the whole file when it carries no per-class section) so existing
        # readers of `soul_prompt` keep seeing the historical value.
        self.soul_text = ""
        self.soul_prompt = ""
        soul_path = os.path.join(self.yaha_dir, "SOUL.md")
        if os.path.exists(soul_path):
            self.soul_text = load_md_with_includes(soul_path)
            self.soul_prompt = soul_for_class(self.soul_text, None, None)
        
    def _stdin_controller(self):
        """The engine's shared stdin/Esc controller (created on demand).

        Never installed outside a root run, so harness / non-interactive
        engines hand agents an inert shared object.
        """
        if self._stdin_interrupt is None:
            self._stdin_interrupt = StdinInterruptController()
        return self._stdin_interrupt

    def resolve_agent_class(self, agent_class_name):
        """Return the AgentClassConfig for a class name or short code, or None.

        Public wrapper so callers outside the engine (the CLI) do not have to
        reach into a private method; internal callers keep using
        ``_resolve_agent_class``.
        """
        return self._resolve_agent_class(agent_class_name)

    def _resolve_agent_class(self, agent_class_name):
        # Resolve by class name first, then by short code.
        cfg = self.agent_classes.get(agent_class_name)
        if cfg:
            return cfg
        for candidate in self.agent_classes.values():
            if candidate.code == agent_class_name:
                return candidate
        return None

    def _class_is_authorized(self, parent_config, target_config):
        # A child class is authorized when it appears in the parent's Agentset
        # (matched by name or short code) or is the parent's own class.
        if parent_config is None or target_config is None:
            return False
        if target_config.name == parent_config.name:
            return True
        for name in parent_config.agentset:
            if name == target_config.name:
                return True
            cfg = self.agent_classes.get(name)
            if cfg and cfg.code == target_config.code:
                return True
        return False

    def _make_agent(self, agent_config, caller_id, recursion_depth):
        # Build a fully wired Agent (engine callbacks + token tracking).
        agent = Agent(
            agent_config=agent_config,
            session=self.session,
            caller_id=caller_id,
            models_config=self.models_config,
            agent_classes=self.agent_classes,
            tool_schemas=self.tool_schemas,
            soul_prompt=soul_for_class(
                self.soul_text,
                getattr(agent_config, "name", None),
                getattr(agent_config, "code", None),
            ),
            verbose=self.verbose,
            access_controller=self.access_controller,
            unattended=self.unattended
        )
        # Every agent this engine makes shares the one stdin/Esc controller:
        # root, runtime peers, and nested children alike.
        agent.stdin_interrupt = self._stdin_controller()

        def spawn_agent_impl(system, instruction, agent_class):
            target_class = agent_class
            if target_class.lower() == "inherited":
                target_class = agent_config.name

            authorized = False
            if target_class in agent_config.agentset:
                authorized = True
            else:
                for name in agent_config.agentset:
                    cfg = self.agent_classes.get(name)
                    if cfg and cfg.code == target_class:
                        authorized = True
                        break

            if not authorized and target_class != agent_config.name:
                return f"Error: Agent class '{agent_class}' is not authorized for this agent. Authorized: {agent_config.agentset} or 'Inherited'."

            return self.run_agent(
                target_class,
                instruction,
                caller_id=agent.agent_id,
                recursion_depth=recursion_depth + 1,
                parent_toolset=agent_config.toolset
            )

        agent.yaha_spawn_agent = spawn_agent_impl

        def google_search_impl(query):
            from ..llm.gemini import GeminiProvider
            from ..config.models import ModelConfig
            search_model_cfg = ModelConfig(name="gemini_search", provider="gemini", model="web-search")
            provider = GeminiProvider(search_model_cfg)

            content, usage, gm = provider.generate([{"role": "user", "content": query}])

            track_tokens("gemini_search", usage)

            if not gm:
                return content

            result = content + "\n\nSources:\n"
            for i, chunk in enumerate(gm.get("chunks", []), 1):
                result += f"[{i}] {chunk['title']} - {chunk['uri']}\n"
            return result

        agent.google_search_tool = google_search_impl

        def track_tokens(model_name, usage):
            if model_name not in self.token_usage:
                self.token_usage[model_name] = {"input": 0, "output": 0}
            self.token_usage[model_name]["input"] += usage.get("prompt_tokens", 0)
            self.token_usage[model_name]["output"] += usage.get("completion_tokens", 0)

        agent.track_tokens = track_tokens
        return agent

    def _wire_parallel_spawner(self, agent, agent_config, runtime):
        # Give one agent a working spawn_parallel_agent bound to this runtime.
        # Called for the root AND for every peer, so the agents are true flat
        # peers: any of them may start further peers, still bound by its OWN
        # Agentset authorization and the toolset privilege cap enforced below.
        def spawn_parallel_impl(agent_class, clone_context="new", prompt=None):
            return self._spawn_parallel_agent(
                runtime, agent, agent_config, agent_class, clone_context, prompt)

        agent.spawn_parallel_agent = spawn_parallel_impl

    def _spawn_parallel_agent(self, runtime, parent_agent, parent_config,
                              agent_class, clone_context="new", prompt=None):
        # Register a new peer agent on the cooperative runtime (non-blocking).
        if runtime is None or parent_agent is None:
            return "Error: spawn_parallel_agent requires an active multi-agency runtime."
        mode = str(clone_context or "new").strip().lower()
        if mode in ("", "inherit"):
            mode = "clone"
        if mode not in ("new", "clone"):
            return "Error: clone_context must be 'new' or 'clone'."

        if isinstance(agent_class, str) and agent_class.lower() in ("inherited", "self"):
            target = parent_config
        else:
            target = self._resolve_agent_class(agent_class)
        if target is None:
            return f"Error: Agent class '{agent_class}' not found."
        if not self._class_is_authorized(parent_config, target):
            return (f"Error: Agent class '{agent_class}' is not authorized for this "
                    f"agent. Authorized: {parent_config.agentset} or 'Inherited'.")
        unauthorized = [t for t in target.toolset if t not in parent_config.toolset]
        if unauthorized:
            return (f"Error: Sub-agent '{target.name}' requested unauthorized tools: "
                    f"{unauthorized}. Sub-agents cannot have broader privileges than "
                    f"their parents.")

        child = self._make_agent(target, caller_id=parent_agent.agent_id,
                                 recursion_depth=0)
        child.runtime = runtime
        # Flat peers: the new peer can itself start further peers.
        self._wire_parallel_spawner(child, target, runtime)
        seed = None
        if mode == "clone":
            import copy as _copy
            seed = _copy.deepcopy(parent_agent.history)

        handle = runtime.spawn(child, prompt or "", seed_history=seed)
        # Return the peer's handle NAME only. Peers coordinate purely through
        # named events: no event is set automatically when the peer finishes,
        # and there is no handle-targeted signalling. Fan-out is unbounded
        # (bounded only by Agentsets and the toolset privilege cap above).
        return (f"Spawned parallel agent '{handle.name}' ({target.name}, "
                f"{mode} context).")

    def run_agent(self, agent_class_name, task_prompt, caller_id=None, recursion_depth=0, parent_toolset=None):
        # Enforce Recursion Limit
        if recursion_depth > 5:
            return "Error: Maximum recursion depth (5) reached. Aborting to prevent infinite loop."

        agent_config = self._resolve_agent_class(agent_class_name)
        if not agent_config:
            return f"Error: Agent class '{agent_class_name}' not found."

        # Enforce Toolset Privilege Cap (Inheritance check)
        if parent_toolset is not None:
             unauthorized_tools = [t for t in agent_config.toolset if t not in parent_toolset]
             if unauthorized_tools:
                  return f"Error: Sub-agent '{agent_config.name}' requested unauthorized tools: {unauthorized_tools}. Sub-agents cannot have broader privileges than their parents."

        agent = self._make_agent(agent_config, caller_id, recursion_depth)

        # Multi-agency runtime: only the root agent (started by the CLI) creates
        # the runtime; every agent on it can then spawn parallel peers. Nested
        # synchronous spawns (yaha_spawn_agent) keep the plain blocking run.
        is_root = caller_id is None
        runtime = None
        if is_root:
            missing = validate_wait_events(self.agent_classes)
            if missing:
                return ("Error: Wait Event(s) with no producer: %s. Add a "
                        "set_event_success/set_event_loop/set_event_jump call "
                        "naming the event or "
                        "fix the Wait Event spelling." % ", ".join(missing))
            runtime = AgentRuntime(ui=agent.ui)
            self._wire_parallel_spawner(agent, agent_config, runtime)

        should_wire_interrupt = is_root
        if should_wire_interrupt:
            # The engine (not any agent) owns stdin for the whole run: the
            # controller outlives every agent's chain, including the root's.
            self._stdin_controller().install()
        try:
            if runtime is not None:
                handle = runtime.spawn(agent, task_prompt, name="main", tag="main")
                try:
                    return runtime.run(handle)
                except RuntimeDeadlockError as exc:
                    agent.ui.display_error(agent_config.name, agent.agent_id, str(exc))
                    return "Error: " + str(exc)
            # Nested (synchronous) sub-agent: there is no runtime here, so a
            # Wait Event can never be satisfied. Surface that as a clear error
            # instead of a bare tool-execution string.
            try:
                return agent.run(task_prompt)
            except RuntimeDeadlockError as exc:
                return ("Error: sub-agent '%s' blocked on a Wait Event but ran "
                        "synchronously, so nothing can set that event. Event-based "
                        "coordination needs spawn_parallel_agent from a root run. [%s]"
                        % (agent_config.name, exc))
        finally:
            if should_wire_interrupt:
                self._stdin_controller().stop()
            if is_root:
                # Clear the multi-agent header state so a later, non-runtime run
                # cannot show a stale 'agents N | active: ...' suffix.
                reset_multi_agent_state()

    def get_token_usage_report(self):
        reports = []
        for model, usage in self.token_usage.items():
            reports.append(f"{model} (input: {usage['input']}, output: {usage['output']})")
        return "Token usage: " + " | ".join(reports)

    def get_sessions_list(self):
        logs_dir = os.path.join(self.yaha_dir, "logs")
        if not os.path.exists(logs_dir):
            return []
        
        sessions = []
        folder_names = sorted([d for d in os.listdir(logs_dir) if os.path.isdir(os.path.join(logs_dir, d))])
        
        for folder in folder_names:
            task = "No task info"
            folder_path = os.path.join(logs_dir, folder)
            # Find the first log file to extract task info
            log_files = [f for f in os.listdir(folder_path) if f.endswith(".md")]
            if log_files:
                # Sort to try and find the main agent log first (usually shortest name or first created)
                log_files.sort()
                log_path = os.path.join(folder_path, log_files[0])
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        line_count = 0
                        for line in f:
                            line_count += 1
                            if line_count > 20: # Optimization: only check first 20 lines
                                break
                            if line.startswith("task: "):
                                task = line[6:].strip()
                                # Truncate task for listing if it's too long
                                if len(task) > 100:
                                    task = task[:97] + "..."
                                break
                except:
                    pass
            sessions.append((folder, task))
            
        return sessions
