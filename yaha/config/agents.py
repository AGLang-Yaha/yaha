# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import re
from ..core.md_loader import load_md_with_macros
from ..hooks.grammar import HOOK_NAMES, parse_stage_hooks, relabel_hooks

# Stage list fields that support a per-block `- **Inherited:** All` marker.
_LIST_KEYS = ("allowed_tools", "required_tools", "exit_tools")

# Stage metadata fields that merge with the parent stage when the stage
# declares `- **Inherited:** All` (the "I did not author it" scalar rule).
_SCALAR_KEYS = ("final_answer", "fail_target", "max_turns", "wait_event",
                "snap", "detached")

# `- **Allowed Tools:**` ... up to the next sibling metadata bullet. The
# negative lookahead keeps a per-block `- **Inherited:** All` marker inside the
# body instead of letting it terminate the list.
_LIST_BODY_RE = r'^\s*-\s*\*\*%s:\*\*\s*(.*?)(?=^\s*-\s*\*\*\s*(?!Inherited)|\Z)'

_INHERIT_MARKER_RE = re.compile(r'^\s*-\s*\*\*\s*Inherited\s*:\*\*\s*([A-Za-z]+)\s*$',
                                re.IGNORECASE)


def _inherit_marker(text):
    """Return True/False for an `Inherited: All|None` bullet, else None."""
    m = _INHERIT_MARKER_RE.match(text)
    if not m:
        return None
    return m.group(1).strip().lower() == "all"


def _blank_regions(block, labels):
    """Blank the body of the given `- **Label:**` regions, keeping line count.

    Used before the stage-level `Inherited: All` probe so that a per-block
    marker can never be mistaken for the stage-level flag.
    """
    for label in labels:
        block = re.sub(
            _LIST_BODY_RE % label,
            lambda m: "\n" * m.group(0).count("\n"),
            block, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
    return block


class AgentClassConfig:
    def __init__(self, name, code, model, system_prompt, toolset, skillset, agentset, 
                 prompt_chain=None, # List of {"name": str, "content": str, "inherit": bool}
                 inherited=None, 
                 inherit_prompt=False, inherit_tools=False, inherit_skills=False, inherit_agents=False,
                 skillset_mode=None, description="", max_turns=100, max_loops=-1,
                 snap_handler=None, snap_threshold=None, snap_to=None, detached_stages=None,
                 hook_timeout=300,
                 compression_keep_steps=None):
        self.name = name
        self.code = code
        self.model = model
        self.system_prompt = system_prompt
        self.toolset = toolset
        self.skillset = skillset
        self.agentset = agentset
        self.prompt_chain = prompt_chain or []
        self.inherited = inherited
        self.inherit_prompt = inherit_prompt
        self.inherit_tools = inherit_tools
        self.inherit_skills = inherit_skills
        self.inherit_agents = inherit_agents
        self.skillset_mode = skillset_mode
        self.description = description
        self.max_turns = max_turns
        self.max_loops = max_loops
        # Snapshot policy (class-level): optional controlled context snapping.
        self.snap_handler = snap_handler
        self.snap_threshold = float(snap_threshold) if snap_threshold is not None else None
        self.snap_to = float(snap_to) if snap_to is not None else None
        # How many recent ReAct steps a context compression keeps (agent-wide).
        # None means "not authored here" so a parent class's value can inherit.
        if compression_keep_steps is None:
            self.compression_keep_steps = None
        else:
            try:
                self.compression_keep_steps = max(0, int(compression_keep_steps))
            except (TypeError, ValueError):
                self.compression_keep_steps = None
        self.detached_stages = dict(detached_stages or {})
        # Stage-hook policy: per-block timeout in seconds for bash and python
        # hooks (0 disables the timeout). None means "not authored here" so the
        # parent's value can be inherited; the final default is applied during
        # inheritance resolution. Precedence at run time:
        #   YAHA_HOOK_TIMEOUT (env) > this value > 300 s default,
        # i.e. the environment variable overrides whatever was authored here
        # (HookEngine._timeout reads the environment first).
        if hook_timeout is None:
            self.hook_timeout = None
        else:
            try:
                self.hook_timeout = float(hook_timeout)
            except (TypeError, ValueError):
                self.hook_timeout = None

def _ratio(value):
    """Normalize authored % or 0-1 value to a float ratio."""
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith("%"):
        return float(text[:-1]) / 100.0
    parsed = float(text)
    return parsed / 100.0 if parsed > 1 else parsed


def parse_agent_class_md(file_path, extra_files=()):
    """Parse the primary AgentClass file plus any EXTRA class files.

    Each file is treated as its OWN document: it is loaded and macro-expanded on
    its own (its own macro store, its own `<!--include-->`s resolved relative to
    itself), parsed into its own class map, and only THEN are the maps merged -
    the primary first, each extra after it, a later definition winning a name
    clash WHOLESALE. Inheritance is resolved once over the merged map, so a class
    in an extra file can inherit from the workspace's classes and from classes in
    earlier extra files.

    This is deliberately NOT what `<!--include-->` does: an include is macro
    processing that inlines TEXT into one document sharing one macro tree, and
    with it both files become one.

    A missing main file still yields an empty map, and a missing EXTRA file is
    skipped: `-I` never turns a missing path into an error at THIS level (the CLI
    validates its own paths before calling in).

    Use `parse_agent_class_documents` when you also want to know WHERE each class
    was defined, and `class_definition_sources` for the clash report.
    """
    return load_agent_classes(file_path, extra_files)[0]


def parse_agent_class_documents(paths):
    """Parse each path as its OWN document; return ``(merged, sources)``.

    `sources` is an ordered ``{name: [(path, count), ...]}`` recording every
    document that defines a class name and how many times THAT document defines
    it, so an internal duplicate and a cross-document clash are both visible.
    """
    merged = {}
    sources = {}
    for path in (paths or ()):
        if not path or not os.path.exists(str(path)):
            continue
        definitions = _parse_class_sections(load_md_with_macros(str(path)))
        counts = {}
        for name, config in definitions:
            merged[name] = config
            counts[name] = counts.get(name, 0) + 1
        for name, count in counts.items():
            sources.setdefault(name, []).append((str(path), count))
    return merged, sources


def load_agent_classes(file_path, extra_files=()):
    """Load the class documents ONCE; return ``(resolved_classes, sources)``.

    This is the single parse path: both the resolved class map and the
    definition report come out of one read of every document, so a caller that
    needs the classes AND the diagnostics (the engine, `--compile`) does not pay
    for a second pass over the files.
    """
    merged, sources = parse_agent_class_documents(
        [file_path] + list(extra_files or ()))
    if not merged:
        return {}, sources
    return _resolve_classes(merged), sources


def class_definition_sources(file_path, extra_files=()):
    """Which files define each class name, and how many times each (ordered)."""
    return load_agent_classes(file_path, extra_files)[1]


def parse_agent_class_text(content):
    """Parse agent classes from markdown TEXT that is already loaded.

    ONE document: the last definition of a name wins, exactly as before.
    """
    return _resolve_classes(dict(_parse_class_sections(content)))


def _parse_class_sections(content):
    """Parse the class sections of ONE document, in order, unresolved.

    Returns a LIST of ``(name, config)`` pairs rather than a mapping, so that a
    document defining the same class twice stays VISIBLE to the caller instead of
    being silently overwritten. Inheritance is resolved later, once, over the
    merged map (see `_resolve_classes`).
    """
    definitions = []
    # Split by ## at the start of a line
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)
    
    for section in sections[1:]:
        if not section.strip():
            continue
            
        lines = section.split('\n')
        name = lines[0].strip()
        section_text = "\n".join(lines[1:])
        
        # 1. Parse Simple Fields
        code_match = re.search(r'^- \*\*Code:\*\*\s*(.*)', section_text, re.MULTILINE)
        code = code_match.group(1).strip() if code_match else ""
        
        description_match = re.search(r'^- \*\*Description:\*\*\s*(.*)', section_text, re.MULTILINE)
        description = description_match.group(1).strip() if description_match else ""
        
        inherited_match = re.search(r'^- \*\*Inherited:\*\*\s*(.*)', section_text, re.MULTILINE)
        inherited = inherited_match.group(1).strip() if inherited_match else None
        
        max_turns_match = re.search(r'^- \*\*Max Turns:\*\*\s*(-?\d+)', section_text, re.MULTILINE)
        max_turns = int(max_turns_match.group(1)) if max_turns_match else 100
        
        max_loops_match = re.search(r'^- \*\*Max Loops:\*\*\s*(-?\d+)', section_text, re.MULTILINE)
        max_loops = int(max_loops_match.group(1)) if max_loops_match else -1
        
        model_match = re.search(r'^- \*\*Model:\*\*\s*`?(.*?)`?$', section_text, re.MULTILINE)
        model = model_match.group(1).strip() if model_match else ""
        
        snap_handler_match = re.search(r'^- \*\*Snap Handler:\*\*\s*(.+)', section_text, re.MULTILINE)
        snap_handler = snap_handler_match.group(1).strip() if snap_handler_match else None
        snap_threshold_match = re.search(r'^- \*\*Snap Threshold:\*\*\s*(.+)', section_text, re.MULTILINE)
        snap_threshold = _ratio(snap_threshold_match.group(1)) if snap_threshold_match else None
        snap_to_match = re.search(r'^- \*\*Snap To:\*\*\s*(.+)', section_text, re.MULTILINE)
        snap_to = _ratio(snap_to_match.group(1)) if snap_to_match else None
        keep_steps_match = re.search(r'^- \*\*Compression Keep Steps:\*\*\s*(\d+)', section_text, re.MULTILINE)
        compression_keep_steps = int(keep_steps_match.group(1)) if keep_steps_match else None

        hook_timeout_match = re.search(r'^- \*\*Hook Timeout:\*\*\s*(-?[\d.]+)',
                                       section_text, re.MULTILINE)
        hook_timeout = float(hook_timeout_match.group(1)) if hook_timeout_match else None
        
        # Helper to extract blockquoted content and stage metadata
        def extract_block(label):
            pattern = rf'^- \*\*{label}:\*\*\s*\n(.*?)(?=\n###|\n- \*\*|\Z)'
            match = re.search(pattern, section_text, re.DOTALL | re.MULTILINE)
            if not match:
                return None

            block = match.group(1)

            # Hook blocks FIRST: their regions are blanked out of the working
            # text, so hook code can never leak into a stage prompt, a tool list
            # or the declared-field detection below.
            hooks, hook_inherit, declared_hooks, block = parse_stage_hooks(block, label)

            # Tool lists accept a per-block marker:
            #   - **Allowed Tools:**
            #       - **Inherited:** All
            #       - read_file
            # meaning "keep the parent's tools and append mine". Without the
            # marker the authored block replaces the parent's list. The marker
            # is stripped from the body so it can never become a tool name.
            def parse_tool_list(text_label):
                m = re.search(_LIST_BODY_RE % text_label, block,
                              re.MULTILINE | re.IGNORECASE | re.DOTALL)
                if not m:
                    return [], False
                items = []
                inherit_all = False
                for line in m.group(1).split('\n'):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    marker = _inherit_marker(stripped)
                    if marker is not None:
                        inherit_all = marker
                        continue
                    if stripped.startswith('-'):
                        items.append(stripped.lstrip('- ').strip())
                    elif not items:
                        # Comma-separated fallback
                        items.extend([x.strip() for x in stripped.split(',') if x.strip()])
                return items, inherit_all

            # The stage-level flag is probed on a copy with the list bodies and
            # the hook regions blanked, so a per-block marker cannot flip it.
            stage_flag_block = _blank_regions(block, ("Required Tools", "Exit Tools",
                                                      "Allowed Tools"))
            inherit = bool(re.search(r'^\s*-\s*\*\*\s*Inherited\s*:\*\*\s*All\s*$',
                                     stage_flag_block, re.MULTILINE | re.IGNORECASE))

            def parse_flag(pattern, default):
                m = re.search(pattern, block, re.MULTILINE | re.IGNORECASE)
                return m.group(1).strip() if m else default

            def parse_bool_flag(pattern):
                m = re.search(pattern, block, re.MULTILINE | re.IGNORECASE)
                return m.group(1).lower() == "true" if m else False

            final_answer = parse_bool_flag(r'^\s*-\s*\*\*Final Answer:\*\*\s*(True|False)')
            detached = parse_bool_flag(r'^\s*-\s*\*\*Detached:\*\*\s*(True|False)')
            snap_raw = parse_flag(r'^\s*-\s*\*\*Snap:\*\*\s*(enabled|disabled)', None)
            snap = snap_raw.lower() if snap_raw else None
            required_tools, required_tools_inherit = parse_tool_list("Required Tools")
            exit_tools, exit_tools_inherit = parse_tool_list("Exit Tools")
            allowed_tools, allowed_tools_inherit = parse_tool_list("Allowed Tools")
            # The value may sit on this line or on the next one, and the match
            # must stop at the next bullet OR at the stage's blockquote prompt.
            # Without the `>` alternative a `Fail Target` written as the LAST
            # bullet before the prompt was silently read as absent - which is
            # also how a hook block could turn a declared target into the
            # default: hook regions are blanked out before this parse, so a
            # legal bullet anchor disappears and only the prompt line remains.
            fail_target = parse_flag(
                r'^\s*-\s*\*\*Fail Target:\*\*\s*(.*?)\s*(?=^\s*-\s*\*\*|^\s*>|\Z)',
                None)
            max_turns_str = parse_flag(r'^\s*-\s*\*\*Max Turns:\*\*\s*(-?\d+)', None)
            max_turns = int(max_turns_str) if max_turns_str else None
            wait_event = parse_flag(r'^\s*-\s*\*\*Wait Event:\*\*\s*(.*)', None)
            if wait_event:
                wait_event = wait_event.strip() or None

            # Track which metadata fields were explicitly authored in this stage.
            # The value parsers above return defaults ([]/False/None) for fields
            # that are absent, so merge_stage uses this set to distinguish
            # "not written" from an intentional empty/False override.
            _meta_field_labels = {
                "required_tools": "Required Tools",
                "exit_tools": "Exit Tools",
                "allowed_tools": "Allowed Tools",
                "final_answer": "Final Answer",
                "fail_target": "Fail Target",
                "max_turns": "Max Turns",
                "snap": "Snap",
                "detached": "Detached",
                "wait_event": "Wait Event",
            }
            declared_fields = {
                key for key, label in _meta_field_labels.items()
                if re.search(r'^\s*-\s*\*\*' + label + r':\*\*', block,
                             re.MULTILINE | re.IGNORECASE)
            }
            # A declared hook block counts as authored even when it is empty, so
            # "written but empty" stays distinguishable from "not written".
            declared_fields |= set(declared_hooks)

            # Per-block `Inherited: All` markers (lists and hooks). They work on
            # their own: they do not require the stage-level flag.
            field_inherit = {
                "required_tools": required_tools_inherit,
                "exit_tools": exit_tools_inherit,
                "allowed_tools": allowed_tools_inherit,
            }
            for _hook_name in HOOK_NAMES:
                field_inherit[_hook_name] = bool(hook_inherit.get(_hook_name))

            prompt_lines = []
            for line in block.split('\n'):
                if line.strip().startswith(">"):
                    prompt_lines.append(line.strip().lstrip("> ").strip())
                elif line.strip() == "" and prompt_lines:
                    prompt_lines.append("")

            return {
                "name": label,
                "content": "\n".join(prompt_lines).strip(),
                "inherit": inherit,
                "final_answer": final_answer,
                "required_tools": required_tools,
                "exit_tools": exit_tools,
                "allowed_tools": allowed_tools,
                "fail_target": fail_target,
                "max_turns": max_turns,
                "wait_event": wait_event,
                "snap": snap,
                "detached": bool(detached),
                "declared_fields": declared_fields,
                "hooks": {name: list(specs) for name, specs in hooks.items()},
                "field_inherit": field_inherit,
            }

        # 2. Parse Prompt Stages
        # We find all top-level labels
        all_labels = re.findall(r'^- \*\*(.*?):\*\*', section_text, re.MULTILINE)
        
        system_prompt = ""
        inherit_prompt = True
        prompt_chain = []
        
        reserved_simple = {"Code", "Description", "Inherited", "Model",
                         "Snap Handler", "Snap Threshold", "Snap To",
                         "Hook Timeout"}
        reserved_special = {"Toolset", "Skillset", "Agentset"}
        
        for label in all_labels:
            if label in reserved_simple or label in reserved_special:
                continue
            
            stage = extract_block(label)
            if stage is None: continue

            if label.lower() == "system prompt":
                system_prompt = stage["content"]
                inherit_prompt = stage["inherit"]
            else:
                prompt_chain.append(stage)

        # 3b. Collect class-level snap policy fields
        # (processed as simple fields above; kept here for clarity).

        # 3. Parse Special Blocks (Toolset, Skillset, Agentset)
        def parse_list_block(label):
            pattern = rf'^(?:### {label}|^- \*\*{label}:\*\*).*?\n(.*?)(?=\n###|\n- \*\*|\Z)'
            match = re.search(pattern, section_text, re.DOTALL | re.MULTILINE)
            if not match:
                return [], True # Missing means inherit
            
            inherit = False
            block = match.group(1)
            if re.search(r'\*\*Inherited:\*\*\s*All', block):
                inherit = True
            
            items = []
            for line in block.split('\n'):
                # Top level list items
                if line.startswith("- ") or (line.strip().startswith("- ") and not re.match(r'^\s+', line)):
                    item = line.strip().lstrip("- ").strip()
                    if not re.search(r'\*\*Inherited:\*\*', item):
                        items.append(item)
                # Support slightly indented items if they don't look like metadata
                elif line.lstrip().startswith("- ") and not re.search(r'\*\*Description:\*\*|\*\*Prototype:\*\*|\*\*Mode:\*\*', line):
                    item = line.strip().lstrip("- ").strip()
                    if "**" not in item and not re.search(r'\*\*Inherited:\*\*', item):
                        items.append(item)
            return items, inherit

        toolset, inherit_tools = parse_list_block("Toolset")
        skillset, inherit_skills = parse_list_block("Skillset")
        agentset, inherit_agents = parse_list_block("Agentset")
        
        # Skillset Mode
        skillset_mode = None
        skillset_match = re.search(r'^(?:### Skillset|^- \*\*Skillset:\*\*).*?\n(.*?)(?=\n###|\n- \*\*|\Z)', section_text, re.DOTALL | re.MULTILINE)
        if skillset_match:
            mode_match = re.search(r'\*\*Mode:\*\*\s*(.*)', skillset_match.group(1))
            if mode_match:
                skillset_mode = mode_match.group(1).strip()

        # Clean agentset (remove "Agent: " prefix)
        agentset = [a.replace("Agent: ", "").strip() if a.startswith("Agent: ") else a for a in agentset]

        definitions.append((name, AgentClassConfig(
            name=name, code=code, model=model, system_prompt=system_prompt,
            toolset=toolset, skillset=skillset, agentset=agentset, 
            prompt_chain=prompt_chain,
            inherited=inherited,
            inherit_prompt=inherit_prompt, inherit_tools=inherit_tools,
            inherit_skills=inherit_skills, inherit_agents=inherit_agents,
            skillset_mode=skillset_mode, description=description,
            max_turns=max_turns, max_loops=max_loops,
            snap_handler=snap_handler, snap_threshold=snap_threshold, snap_to=snap_to,
            compression_keep_steps=compression_keep_steps,
            hook_timeout=hook_timeout
        )))

    return definitions


def _resolve_classes(agent_classes):
    """Resolve inheritance and field defaults over a merged class map.

    `agent_classes` maps a class name to its UNRESOLVED config; the last
    definition of a name is the one resolved. Returns the resolved mapping.
    """
    # 4. Resolve Inheritance
    resolved_classes = {}
    
    def resolve(name):
        if name is None or name == "None":
            return None
        if name in resolved_classes:
            return resolved_classes[name]
        
        if name not in agent_classes:
            return None
            
        config = agent_classes[name]
        parent = None
        if config.inherited and config.inherited != "None":
            parent = resolve(config.inherited)
            if parent:
                if not config.model:
                    config.model = parent.model
                
                if config.max_turns == 100 and parent.max_turns != 100:
                    config.max_turns = parent.max_turns
                
                if config.max_loops == -1 and parent.max_loops != -1:
                    config.max_loops = parent.max_loops
                
                if not config.description:
                    config.description = parent.description

                if config.inherit_tools:
                    config.toolset = list(set(parent.toolset + config.toolset))
                
                if config.inherit_skills:
                    config.skillset = list(set(parent.skillset + config.skillset))
                
                if config.inherit_agents:
                    config.agentset = list(set(parent.agentset + config.agentset))
                
                if config.inherit_prompt:
                    config.system_prompt = (parent.system_prompt + "\n\n" + config.system_prompt).strip()

                # Resolve prompt chain inheritance (Straightened Logic)
                parent_chain = parent.prompt_chain
                child_chain = config.prompt_chain

                def find_stage(chain, name):
                    return next((s for s in chain if s["name"].lower() == name.lower()), None)

                def _merge_list(child, parent, key, getter):
                    """Merge one list field (a tool list or a hook list).

                    The rule is uniform - `Inherited: All` keeps and appends,
                    anything else replaces:

                    * the stage-level `- **Inherited:** All` inherits the
                      parent's list when the child did not author that field at
                      all (so lists inherit exactly like scalars);
                    * a per-block `- **Inherited:** All` marker inside the block
                      appends the child's items AFTER the parent's, whether or
                      not the stage-level flag is set (the marker is
                      self-sufficient);
                    * otherwise the authored block replaces the parent's list.

                    Returns ``(items, keep)`` where ``keep`` says whether the
                    result must stay "present" so it remains re-inheritable
                    further down the chain.
                    """
                    child_items = list(getter(child, key) or [])
                    child_declared = key in child.get("declared_fields", ())
                    parent_items = list(getter(parent, key) or []) if parent else []
                    if not parent_items:
                        return child_items, child_declared
                    per_block = bool((child.get("field_inherit") or {}).get(key))
                    stage_flag = bool(child.get("inherit")) and parent is not None
                    if per_block or (stage_flag and not child_declared):
                        return parent_items + child_items, True
                    return child_items, child_declared

                def merge_stage(child, parent=None):
                    merged = {**child, "inherit": False}
                    child_declared = set(child.get("declared_fields", ()))
                    merged["hooks"] = {name: list(specs)
                                       for name, specs in (child.get("hooks") or {}).items()}
                    merged["field_inherit"] = dict(child.get("field_inherit") or {})
                    derived = set()

                    if parent and child.get("inherit"):
                        # 'Inherited: All' means: inherit the parent stage's
                        # SCALAR metadata too, but only for fields the child did
                        # not explicitly author. 'declared_fields' records the
                        # authored fields, so a child can deliberately override
                        # the parent (e.g. Final Answer: False).
                        parent_declared = set(parent.get("declared_fields", ()))
                        inherited_keys = {
                            k for k in _SCALAR_KEYS
                            if k not in child_declared and k in parent_declared
                        }
                        for key in inherited_keys:
                            merged[key] = parent.get(key)
                        merged["content"] = (parent["content"] + "\n\n" + child["content"]).strip()
                        derived |= inherited_keys

                    # Lists and hooks follow the uniform rule above. The parent
                    # stage is looked up by name even when the stage-level flag
                    # is absent, because a per-block marker is self-sufficient.
                    for key in _LIST_KEYS:
                        items, keep = _merge_list(child, parent, key,
                                                  lambda stage, k: stage.get(k))
                        merged[key] = items
                        if keep:
                            derived.add(key)
                    for key in HOOK_NAMES:
                        items, keep = _merge_list(
                            child, parent, key,
                            lambda stage, k: (stage.get("hooks") or {}).get(k))
                        merged["hooks"][key] = items
                        if keep:
                            derived.add(key)
                    merged["hooks"] = relabel_hooks(merged["hooks"])

                    # Inline the actually-inherited keys into declared_fields: a
                    # resolved stage is itself the parent of the next
                    # generation, so fields it picked up from an ancestor must
                    # stay "present" to remain re-inheritable further down.
                    merged["declared_fields"] = child_declared | derived
                    return merged

                p_preamble = find_stage(parent_chain, "Preamble")
                p_aftermath = find_stage(parent_chain, "Aftermath")

                c_preamble = find_stage(child_chain, "Preamble")
                c_aftermath = find_stage(child_chain, "Aftermath")

                # 1. Resolve Preamble (Mandatory, First)
                final_preamble = None
                if c_preamble:
                    final_preamble = merge_stage(c_preamble, p_preamble)
                elif p_preamble:
                    final_preamble = {**p_preamble, "inherit": False}

                # 2. Resolve Aftermath (Mandatory, Last)
                final_aftermath = None
                if c_aftermath:
                    final_aftermath = merge_stage(c_aftermath, p_aftermath)
                elif p_aftermath:
                    final_aftermath = {**p_aftermath, "inherit": False}

                # 3. Resolve Custom Stages (ONLY those declared in child, in child's order)
                final_custom_stages = []
                for c_stage in child_chain:
                    stage_name = c_stage["name"]
                    if stage_name.lower() in ["preamble", "aftermath"]:
                        continue

                    # The same-named parent stage is looked up even when this
                    # stage does not declare `Inherited: All`, because a per-block
                    # `- **Inherited:** All` marker is self-sufficient: a single
                    # list or hook block can extend the parent's without the
                    # stage inheriting anything else. merge_stage keeps the
                    # stage-level flag as the gate for scalars and prompt text.
                    p_stage = find_stage(parent_chain, stage_name)
                    final_custom_stages.append(merge_stage(c_stage, p_stage))

                # 4. Construct Final Chain (detached stages are filtered later)
                new_chain = []
                if final_preamble:
                    new_chain.append(final_preamble)
                new_chain.extend(final_custom_stages)
                if final_aftermath:
                    new_chain.append(final_aftermath)

                # 5. Detached stage inheritance:
                #    A detached stage is declared but NOT part of the ordinary
                #    chain. Every class inherits the parent's resolved detached
                #    stage set; a same-name detached stage authored on this class
                #    overrides/merges the inherited one (normal stage semantics).
                effective_detached = {}
                if parent is not None:
                    for dname, dstage in (parent.detached_stages or {}).items():
                        effective_detached[dname] = dict(dstage)
                # 5b. Overrides authored on this class.
                for c_stage in child_chain:
                    if not c_stage.get("detached", False):
                        continue
                    cname = c_stage["name"]
                    p_det = effective_detached.get(cname)
                    merged = merge_stage(c_stage, p_det) if p_det else dict(c_stage, inherit=False)
                    effective_detached[cname] = merged

                config.detached_stages = effective_detached
                config.prompt_chain = [s for s in new_chain if not s.get("detached", False)]

                # 6. Class-level snap policy inherits when omitted.
                if config.snap_handler is None:
                    config.snap_handler = parent.snap_handler
                if config.snap_threshold is None:
                    config.snap_threshold = parent.snap_threshold
                if config.snap_to is None:
                    config.snap_to = parent.snap_to
                if config.compression_keep_steps is None:
                    config.compression_keep_steps = parent.compression_keep_steps
                if config.skillset_mode is None:
                    config.skillset_mode = parent.skillset_mode
                if config.hook_timeout is None:
                    config.hook_timeout = parent.hook_timeout

        if parent is None and config.inherited in (None, "None"):
            # Root class: no inheritance merge, but detached stages still need
            # to be split out of the ordinary chain so descendants inherit them
            # separately.
            own_detached = {}
            for own_stage in config.prompt_chain:
                if own_stage.get("detached", False):
                    own_detached[own_stage["name"]] = dict(own_stage, inherit=False)
            config.detached_stages = {**config.detached_stages, **own_detached}
            config.prompt_chain = [s for s in config.prompt_chain if not s.get("detached", False)]

        resolved_classes[name] = config
        return config

    for name in list(agent_classes.keys()):
        resolve(name)

    # Stage hooks are opt-in per class: a class that never authored `Hook
    # Timeout` and has no ancestor that did falls back to the default.
    for config in resolved_classes.values():
        if config.hook_timeout is None:
            config.hook_timeout = 300.0

    return resolved_classes
