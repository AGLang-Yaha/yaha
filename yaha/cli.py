import sys
import os
import argparse
import shutil
import importlib.resources
import readline
import subprocess
import re
from datetime import datetime
from yaha import __version__
from yaha.core.engine import YAHAEngine
from yaha.core.md_loader import MarkdownLoadError

def get_template_path(filename):
    """Returns the path to a template file bundled with the package."""
    try:
        # Modern way (Python 3.9+)
        traversable = importlib.resources.files("yaha.templates").joinpath(filename)
        return str(traversable)
    except Exception as e:
        # Fallback for older environments if needed
        try:
            with importlib.resources.path("yaha.templates", filename) as p:
                return str(p)
        except Exception as inner_e:
            print(f"Warning: Could not find template {filename}: {inner_e}")
            return ""


# Entries in the bundled templates dir that are NOT shipped to .yaha/ by the
# wholesale top-level copy: packaging cruft, and subdirectories (skills/,
# shellcopilot/, obsidian/) which have their own installers below.
_TEMPLATE_COPY_EXCLUDE = {"__init__.py", "__pycache__", ".DS_Store"}


def list_template_files():
    """Names of every top-level file in the bundled templates dir.

    init/upgrade ship ALL of them to .yaha/ instead of a per-file
    whitelist, so adding a template (e.g. const.md, included by
    AgentClass.md) never strands existing or new workspaces.
    """
    try:
        root = importlib.resources.files("yaha.templates")
        return sorted(entry.name for entry in root.iterdir()
                      if entry.is_file()
                      and entry.name not in _TEMPLATE_COPY_EXCLUDE)
    except Exception as e:
        print(f"Warning: Could not list template files: {e}")
        return []

def init_workspace(agent_name="BaseAgent"):
    yaha_dir = ".yaha"
    os.makedirs(os.path.join(yaha_dir, "skills"), exist_ok=True)
    os.makedirs(os.path.join(yaha_dir, "resources"), exist_ok=True)
    memory_dir = os.path.join(yaha_dir, "memory")
    os.makedirs(memory_dir, exist_ok=True)
    
    # Initialize git in memory folder if not already a repo
    if not os.path.exists(os.path.join(memory_dir, ".git")):
        try:
            subprocess.run(["git", "init"], cwd=memory_dir, capture_output=True, check=True)
            # Add a basic .gitignore to the sub-repo
            with open(os.path.join(memory_dir, ".gitignore"), "w", encoding="utf-8") as f:
                f.write(".DS_Store\n")
            print(f"Initialized git repository in {memory_dir}")
        except Exception as e:
            print(f"Warning: Could not initialize git in {memory_dir}: {e}")
    
    logs_dir = os.path.join(yaha_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    # Initialize git in logs folder if not already a repo
    if not os.path.exists(os.path.join(logs_dir, ".git")):
        try:
            subprocess.run(["git", "init"], cwd=logs_dir, capture_output=True, check=True)
            # Add a .gitignore that ignores all log folders by default
            with open(os.path.join(logs_dir, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("*\n!.gitignore\n")
            print(f"Initialized git repository in {logs_dir}")
        except Exception as e:
            print(f"Warning: Could not initialize git in {logs_dir}: {e}")
    
    # Ship EVERY top-level template file to .yaha/ (never overwriting
    # existing files): a new template then needs no whitelist update.
    # SOUL.md is the one special case - it is personalized with the
    # agent name on the way in.
    for filename in list_template_files():
        dest = os.path.join(yaha_dir, filename)
        if os.path.exists(dest):
            continue
        source = get_template_path(filename)
        if not source or not os.path.exists(source):
            continue
        if filename == "SOUL.md":
            with open(source, "r", encoding="utf-8") as f:
                content = f.read().replace("{agent_name}", agent_name)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            shutil.copy(source, dest)

    # Minimal SOUL.md fallback if the template itself is missing.
    soul_path = os.path.join(yaha_dir, "SOUL.md")
    if not os.path.exists(soul_path):
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(f"# SOUL\n\nYou are {agent_name}, a helpful and efficient agent.\n")

    # Copy Obsidian templates to workspace root .obsidian/
    workspace_obsidian_dir = ".obsidian"
    os.makedirs(workspace_obsidian_dir, exist_ok=True)
    for filename in ["app.json", "appearance.json", "core-plugins.json", "graph.json", "community-plugins.json"]:
        dest_file = os.path.join(workspace_obsidian_dir, filename)
        if not os.path.exists(dest_file):
            source = get_template_path(f"obsidian/{filename}")
            if os.path.exists(source):
                shutil.copy(source, dest_file)
                
    # Recursively copy pre-bundled plugins (e.g. show-hidden-files)
    plugins_source_dir = get_template_path("obsidian/plugins")
    if plugins_source_dir and os.path.exists(plugins_source_dir):
        shutil.copytree(plugins_source_dir, os.path.join(workspace_obsidian_dir, "plugins"), dirs_exist_ok=True)

    # Create ext/ directory for workspace-specific agents
    ext_dir = os.path.join(yaha_dir, "ext")
    os.makedirs(ext_dir, exist_ok=True)
    # Initialize git in ext folder if not already a repo
    if not os.path.exists(os.path.join(ext_dir, ".git")):
        try:
            subprocess.run(["git", "init"], cwd=ext_dir, capture_output=True, check=True)
            # Add a basic .gitignore to the sub-repo
            with open(os.path.join(ext_dir, ".gitignore"), "w", encoding="utf-8") as f:
                f.write(".DS_Store\n")
            print(f"Initialized git repository in {ext_dir}")
        except Exception as e:
            print(f"Warning: Could not initialize git in {ext_dir}: {e}")

    # Create workspace-specific default agent
    workspace_name = os.path.basename(os.getcwd())
    # Clean workspace name for use as a class name (replace spaces/dots with underscores)
    safe_name = "".join([c if c.isalnum() else "_" for c in workspace_name])
    
    workspace_md_path = os.path.join(ext_dir, f"{safe_name}.md")
    if not os.path.exists(workspace_md_path):
        with open(workspace_md_path, "w", encoding="utf-8") as f:
            f.write(f"## {safe_name}\n")
            f.write("- **Inherited:** BaseAgent\n")
            # Generate a 3-letter code from the name
            code = (safe_name[:3].capitalize() if len(safe_name) >= 3 else safe_name.capitalize())
            f.write(f"- **Code:** {code}\n")
            f.write(f"- **Description:** Default agent for {workspace_name} workspace.\n")
            f.write("- **System Prompt:**\n")
            f.write("    - **Inherited:** All\n")
            f.write(f"    > You are the default agent for the {workspace_name} workspace.\n")
        
        # Add include tag to AgentClass.md
        agent_path = os.path.join(yaha_dir, "AgentClass.md")
        if os.path.exists(agent_path):
            with open(agent_path, "a", encoding="utf-8") as f:
                f.write(f'\n<!-- include path="ext/{safe_name}.md" -->\n')
    
    # Copy default skills
    try:
        # Use importlib.resources.files for directory access
        skills_template_dir = importlib.resources.files("yaha.templates").joinpath("skills")
        if skills_template_dir.is_dir():
            target_skills_dir = os.path.join(yaha_dir, "skills")
            for skill_entry in skills_template_dir.iterdir():
                if skill_entry.is_dir():
                    skill_name = skill_entry.name
                    dest_skill_dir = os.path.join(target_skills_dir, skill_name)
                    if not os.path.exists(dest_skill_dir):
                        # Recursive copy helper
                        def copy_resource_dir(source_path, dest_path):
                            os.makedirs(dest_path, exist_ok=True)
                            for entry in source_path.iterdir():
                                if entry.is_dir():
                                    copy_resource_dir(entry, os.path.join(dest_path, entry.name))
                                else:
                                    with open(os.path.join(dest_path, entry.name), "wb") as f:
                                        f.write(entry.read_bytes())
                        
                        copy_resource_dir(skill_entry, dest_skill_dir)
                        print(f"Installed default skill: {skill_name}")
    except Exception as e:
        print(f"Warning: Could not install default skills: {e}")

    # Copy shellcopilot template
    try:
        shellcopilot_template_dir = importlib.resources.files("yaha.templates").joinpath("shellcopilot")
        if shellcopilot_template_dir.is_dir():
            target_shellcopilot_dir = os.path.join(yaha_dir, "shellcopilot")
            os.makedirs(target_shellcopilot_dir, exist_ok=True)
            for entry in shellcopilot_template_dir.iterdir():
                if entry.is_file() and entry.name != ".DS_Store":
                    dest_file = os.path.join(target_shellcopilot_dir, entry.name)
                    if not os.path.exists(dest_file):
                        with open(dest_file, "wb") as f:
                            f.write(entry.read_bytes())
            print("Installed shellcopilot template")
    except Exception as e:
        print(f"Warning: Could not install shellcopilot template: {e}")

    print(f"Initialized YAHA workspace with agent: {agent_name}")

def create_skill(skill_name, workspace_dir="."):
    skill_dir = os.path.join(workspace_dir, ".yaha", "skills", skill_name)
    os.makedirs(skill_dir, exist_ok=True)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    
    if os.path.exists(skill_file):
        print(f"Skill '{skill_name}' already exists.")
        return

    content = f"""---
name: {skill_name}
description: Brief explanation of what this expert skill does.
resources:
  - example-resource-1
---

# Instructions
Detailed expert guidance for {skill_name}.
"""
    with open(skill_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Created empty skill: {skill_name} at {skill_file}")

def upgrade_workspace():
    """Synchronizes latest config files and default skills to an existing workspace."""
    yaha_dir = ".yaha"
    if not os.path.exists(yaha_dir):
        print(f"Error: No {yaha_dir} folder found. Use --init to initialize first.")
        return

    # Ensure ext/ exists and is git initialized
    ext_dir = os.path.join(yaha_dir, "ext")
    os.makedirs(ext_dir, exist_ok=True)
    if not os.path.exists(os.path.join(ext_dir, ".git")):
        try:
            subprocess.run(["git", "init"], cwd=ext_dir, capture_output=True, check=True)
            print(f"Initialized git repository in {ext_dir}")
        except:
            pass

    # Synchronize EVERY top-level template file (no per-file whitelist) -
    # except SOUL.md, which is personalized at init time and must never
    # be clobbered by an upgrade.
    sync_files = [f for f in list_template_files() if f != "SOUL.md"]
    
    for filename in sync_files:
        source = get_template_path(filename)
        dest = os.path.join(yaha_dir, filename)
        
        # Special handling for AgentClass.md to preserve and migrate includes
        includes = []
        if filename == "AgentClass.md" and os.path.exists(dest):
            try:
                with open(dest, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("<!-- include"):
                            # Parse path from <!-- include path="FILE" -->
                            match = re.search(r'path="([^"]+)"', line)
                            if match:
                                inc_path = match.group(1)
                                # If it's a file directly in .yaha/, migrate it to ext/
                                old_file_path = os.path.join(yaha_dir, inc_path)
                                if os.path.exists(old_file_path) and not inc_path.startswith("ext/"):
                                    new_inc_path = f"ext/{inc_path}"
                                    new_file_path = os.path.join(yaha_dir, new_inc_path)
                                    os.makedirs(os.path.dirname(new_file_path), exist_ok=True)
                                    shutil.move(old_file_path, new_file_path)
                                    print(f"Migrated agent extension: {inc_path} -> {new_inc_path}")
                                    line = line.replace(f'path="{inc_path}"', f'path="{new_inc_path}"')
                            includes.append(line)
            except Exception as e:
                print(f"Warning: Could not read existing {filename} to preserve/migrate includes: {e}")

        if os.path.exists(source):
            shutil.copy(source, dest)
            
            # Restore/Update includes to AgentClass.md
            if includes and filename == "AgentClass.md":
                try:
                    with open(dest, "a", encoding="utf-8") as f:
                        # Ensure there's a newline before appending
                        f.write("\n")
                        for inc in includes:
                            f.write(inc)
                    print(f"Synchronized {filename} (preserved {len(includes)} includes)")
                except Exception as e:
                    print(f"Warning: Could not restore includes to {filename}: {e}")
            else:
                print(f"Synchronized {filename}")

    # Synchronize default skills
    try:
        # Use importlib.resources.files for directory access
        skills_template_dir = importlib.resources.files("yaha.templates").joinpath("skills")
        if skills_template_dir.is_dir():
            target_skills_dir = os.path.join(yaha_dir, "skills")
            os.makedirs(target_skills_dir, exist_ok=True)
            for skill_entry in skills_template_dir.iterdir():
                if skill_entry.is_dir():
                    skill_name = skill_entry.name
                    dest_skill_dir = os.path.join(target_skills_dir, skill_name)
                    
                    # Recursive copy helper
                    def copy_resource_dir(source_path, dest_path):
                        os.makedirs(dest_path, exist_ok=True)
                        for entry in source_path.iterdir():
                            if entry.is_dir():
                                copy_resource_dir(entry, os.path.join(dest_path, entry.name))
                            else:
                                with open(os.path.join(dest_path, entry.name), "wb") as f:
                                    f.write(entry.read_bytes())
                    
                    copy_resource_dir(skill_entry, dest_skill_dir)
                    print(f"Synchronized skill: {skill_name}")
    except Exception as e:
        print(f"Warning: Could not synchronize default skills: {e}")

    # Synchronize shellcopilot template
    try:
        shellcopilot_template_dir = importlib.resources.files("yaha.templates").joinpath("shellcopilot")
        if shellcopilot_template_dir.is_dir():
            target_shellcopilot_dir = os.path.join(yaha_dir, "shellcopilot")
            os.makedirs(target_shellcopilot_dir, exist_ok=True)
            for entry in shellcopilot_template_dir.iterdir():
                if entry.is_file() and entry.name != ".DS_Store":
                    with open(os.path.join(target_shellcopilot_dir, entry.name), "wb") as f:
                        f.write(entry.read_bytes())
            print("Synchronized shellcopilot template")
    except Exception as e:
        print(f"Warning: Could not synchronize shellcopilot template: {e}")

    # Synchronize Obsidian templates to workspace root .obsidian/
    workspace_obsidian_dir = ".obsidian"
    try:
        os.makedirs(workspace_obsidian_dir, exist_ok=True)
        for filename in ["app.json", "appearance.json", "core-plugins.json", "graph.json", "community-plugins.json"]:
            dest_file = os.path.join(workspace_obsidian_dir, filename)
            if not os.path.exists(dest_file):
                source = get_template_path(f"obsidian/{filename}")
                if os.path.exists(source):
                    shutil.copy(source, dest_file)
                
        # Recursively copy pre-bundled plugins (e.g. show-hidden-files)
        plugins_source_dir = get_template_path("obsidian/plugins")
        if plugins_source_dir and os.path.exists(plugins_source_dir):
            shutil.copytree(plugins_source_dir, os.path.join(workspace_obsidian_dir, "plugins"), dirs_exist_ok=True)
        print("Synchronized Obsidian workspace vault settings and plugins")
    except Exception as e:
        print(f"Warning: Could not synchronize Obsidian workspace configurations: {e}")

    print("Workspace upgraded to the latest YAHA framework configurations.")

def commit_memory_changes(workspace_dir, commit_message):
    """Checks for changes in .yaha/memory and commits them if found."""
    memory_dir = os.path.join(workspace_dir, ".yaha", "memory")
    if not os.path.exists(os.path.join(memory_dir, ".git")):
        return

    # Ensure .gitignore exists in sub-repo
    ignore_file = os.path.join(memory_dir, ".gitignore")
    if not os.path.exists(ignore_file):
        with open(ignore_file, "w", encoding="utf-8") as f:
            f.write(".DS_Store\n")

    try:
        # Stage all changes
        subprocess.run(["git", "add", "."], cwd=memory_dir, capture_output=True, check=True)
        
        # Check if there's anything to commit
        status = subprocess.run(["git", "status", "--short"], cwd=memory_dir, capture_output=True, text=True, check=True)
        
        if status.stdout.strip():
            # Truncate commit message if too long for git
            clean_msg = commit_message.replace("\n", " ").strip()
            if len(clean_msg) > 100:
                clean_msg = clean_msg[:97] + "..."
                
            subprocess.run(["git", "commit", "-m", clean_msg], cwd=memory_dir, capture_output=True, check=True)
            print(f"Committed memory changes: {clean_msg}")
    except Exception as e:
        print(f"Warning: Could not commit memory changes: {e}")

def commit_ext_changes(workspace_dir, commit_message):
    """Checks for changes in .yaha/ext and commits them if found."""
    ext_dir = os.path.join(workspace_dir, ".yaha", "ext")
    if not os.path.exists(os.path.join(ext_dir, ".git")):
        return

    # Ensure .gitignore exists in sub-repo
    ignore_file = os.path.join(ext_dir, ".gitignore")
    if not os.path.exists(ignore_file):
        with open(ignore_file, "w", encoding="utf-8") as f:
            f.write(".DS_Store\n")

    try:
        # Stage all changes
        subprocess.run(["git", "add", "."], cwd=ext_dir, capture_output=True, check=True)
        
        # Check if there's anything to commit
        status = subprocess.run(["git", "status", "--short"], cwd=ext_dir, capture_output=True, text=True, check=True)
        
        if status.stdout.strip():
            # Truncate commit message if too long for git
            clean_msg = commit_message.replace("\n", " ").strip()
            if len(clean_msg) > 100:
                clean_msg = clean_msg[:97] + "..."
                
            subprocess.run(["git", "commit", "-m", clean_msg], cwd=ext_dir, capture_output=True, check=True)
            print(f"Committed extension changes: {clean_msg}")
    except Exception as e:
        print(f"Warning: Could not commit extension changes: {e}")


def default_agent_class(engine):
    """The class a bare `yaha "task"` runs.

    A workspace folder named after one of its classes selects that class;
    otherwise the bundled default, `BaseAgent`, is used.  The run path resolves
    the class through this one definition.
    """
    workspace_name = os.path.basename(os.getcwd())
    safe_name = "".join([c if c.isalnum() else "_" for c in workspace_name])
    if safe_name in engine.agent_classes:
        return safe_name
    return "BaseAgent"


def check_and_trigger_sleep(workspace_dir, engine):
    """Triggers SleepAgent if memory volume exceeds threshold (1.5 * expected capacity)."""
    memory_dir = os.path.join(workspace_dir, ".yaha", "memory")
    if not os.path.exists(memory_dir):
        return
    
    total_lines = 0
    try:
        for f in os.listdir(memory_dir):
            if f.endswith(".md"):
                file_path = os.path.join(memory_dir, f)
                with open(file_path, "r", encoding="utf-8") as file:
                    total_lines += len(file.readlines())
    except Exception as e:
        # print(f"Warning: Error during sleep threshold check: {e}")
        return

    # Rubrics: m=10 files, n=100 lines. w = m * n = 1000.
    # Threshold = 1.5 * w = 1500 lines.
    threshold = 1500
    if total_lines > threshold:
        from yaha.ui.terminal import YAHAUI
        ui = YAHAUI()
        ui.console.print(f"\n[bold blue]SYSTEM: Memory volume ({total_lines} lines) exceeds threshold ({threshold}). Triggering automatic Sleep cycle...[/bold blue]")
        engine.run_agent("SleepAgent", "Perform memory consolidation and ensure rubrics are met (max 10 files, max 100 lines each).")
        # Commit changes made by SleepAgent
        commit_memory_changes(workspace_dir, "Automatic memory consolidation (SleepAgent)")

def main():
    parser = argparse.ArgumentParser(description="YAHA - Yet Another HArness")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-a", "--agent", help="Agent code or name to run")
    parser.add_argument("-F", "--file", help="Provide task via a specific file")
    parser.add_argument("-w", "--workspace", help="Workspace directory", default=".")
    parser.add_argument("-I", "--include",
                        help="Include additional Agent Class file(s). Each is loaded as "
                             "its own document and its classes are merged with the "
                             "workspace's - a later definition of the same name wins "
                             "wholesale, and inheritance resolves across both. A path "
                             "that does not exist is an error. Macros stay per-file: "
                             "install with <!--include--> to share the workspace's "
                             "macro tree. Check a workspace with --compile.",
                        action="append")
    parser.add_argument("--sk-dir", help="Additional skill search folder(s)", action="append")
    parser.add_argument("--init", help="Initialize .yaha workspace", nargs='?', const="BaseAgent")
    parser.add_argument("--upgrade", help="Upgrade .yaha workspace to latest framework configurations", action="store_true")
    parser.add_argument("--create_skill", help="Create an empty skill with the specified name")
    parser.add_argument("--list", help="List items: skills, agents, tools, sessions", choices=["skills", "agents", "tools", "sessions"])
    parser.add_argument("--compile", action="store_true", help="Load and check the agent classes (AgentClass.md plus any -I files), report errors and warnings, then exit without running an agent")
    parser.add_argument("-v", "--verbose", help="Enable verbose TUI mode", action="store_true")
    parser.add_argument("-o", "--output", help="Save the final raw answer to the specified file")
    parser.add_argument("--ide", help="Open the current workspace natively as an Obsidian Vault", action="store_true")
    parser.add_argument("-y", "--yes", help="Grant Yes-for-All for all ACS confirmations", action="store_true")
    parser.add_argument("-f", "--force", help="Force unattended mode (combine with --yes and --output)", action="store_true")
    parser.add_argument("prompt", nargs="*", help="Task prompt")
    
    args = parser.parse_args()
    
    # Pre-resolve relative paths to absolute paths before we potentially chdir
    if args.include:
        args.include = [os.path.abspath(p) for p in args.include]
        # `-I` files are validated HERE, at the command line: a class file that
        # does not exist is a typo to REPORT, not something to skip silently.
        # (The library parser stays permissive; strictness lives at the CLI.)
        missing = [path for path in args.include if not os.path.exists(path)]
        if missing:
            for path in missing:
                print(f"Error: included Agent Class file not found: {path}")
            sys.exit(2)
    if args.sk_dir:
        args.sk_dir = [os.path.abspath(p) for p in args.sk_dir]
    if args.file:
        args.file = os.path.abspath(args.file)
    
    if args.create_skill:
        create_skill(args.create_skill, workspace_dir=args.workspace)
        return

    # Change to workspace if provided
    workspace_root = os.path.abspath(args.workspace)
    os.makedirs(workspace_root, exist_ok=True)
    os.chdir(workspace_root)

    if args.ide:
        import webbrowser
        import urllib.parse
        abs_path = os.path.abspath(".")
        encoded_path = urllib.parse.quote(abs_path)
        obsidian_uri = f"obsidian://open?path={encoded_path}"
        print(f"Opening workspace as Obsidian Vault: {abs_path}")
        webbrowser.open(obsidian_uri)
        return

    if args.init:
        init_workspace(args.init)
        return

    if args.upgrade:
        upgrade_workspace()
        return

    # Determine task prompt. The check-only mode needs no prompt, so it must not
    # touch stdin - that also keeps `--compile` usable inside a pipeline.
    prompt = "\n".join(args.prompt)
    if not sys.stdin.isatty() and not args.compile:
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            prompt = (prompt + "\n" + stdin_content).strip()
    
    if args.file:
        file_path = args.file
        # Check if file exists relative to new CWD
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                prompt = (prompt + "\n" + f.read()).strip()
        else:
            print(f"Error: Task file '{file_path}' not found.")
            sys.exit(1)

    # Initialize Engine (needed for some lists and for running)
    # Unattended automation: explicit -f -y -o or non-TTY stdin means no
    # operator is expected to answer every retry prompt.
    _unattended = bool(args.yes and args.force and args.output) or (not sys.stdin.isatty())
    engine = YAHAEngine(
        workspace_dir=".", 
        include_agent_files=args.include, 
        extra_skill_dirs=args.sk_dir, 
        verbose=args.verbose,
        yes_for_all=args.yes,
        unattended=_unattended
    )

    # Handle the check-only mode: load everything and report, never run an agent.
    # Like --list, this is read-only and needs no task prompt.
    if args.compile:
        from yaha.core.runtime import validate_wait_events
        problems = []
        notes = []
        if not engine.agent_classes:
            problems.append("No agent classes were loaded from .yaha/AgentClass.md "
                            "(or from any -I file).")
        for name, parent in engine.unknown_parents:
            problems.append("Class '%s' inherits from '%s', which is not defined "
                            "anywhere in the workspace." % (name, parent))
        missing_events = validate_wait_events(engine.agent_classes)
        if missing_events:
            problems.append("Wait Event(s) with no producer: %s. Add a "
                            "set_event_success/set_event_loop/set_event_jump call "
                            "naming the event, or fix the Wait Event spelling."
                            % ", ".join(missing_events))
        for name, entries in sorted(engine.class_conflicts.items()):
            detail = ", ".join("%s x%d" % (path, count) for path, count in entries)
            total = sum(count for _path, count in entries)
            notes.append("Class '%s' is defined %d time(s) - %s. The LAST definition "
                         "wins." % (name, total, detail))
        known_models = set(engine.models_config or ())
        for name, config in sorted(engine.agent_classes.items()):
            if config.model and known_models and config.model not in known_models:
                notes.append("Class '%s' uses model '%s', which Model.md does not "
                             "define." % (name, config.model))
            if not (config.code or "").strip():
                notes.append("Class '%s' has an empty Code." % name)
        files = sorted({path for entries in engine.class_sources.values()
                        for path, _count in entries})
        print("Checked %d class(es) from %d file(s)."
              % (len(engine.agent_classes), len(files)))
        for note in notes:
            print("Warning: %s" % note)
        for problem in problems:
            print("Error: %s" % problem)
        if problems:
            sys.exit(1)
        print("OK: 0 errors, %d warning(s)." % len(notes))
        return

    # Handle List command.
    #
    if args.list:
        from yaha.ui.terminal import YAHAUI
        ui = YAHAUI()
        if args.list == "skills":
            import yaml
            skill_roots = engine.session.get_skill_roots()
            
            items = {}
            for root in skill_roots:
                if os.path.exists(root):
                    # Sort to ensure stable order within a directory
                    for skill_name in sorted(os.listdir(root)):
                        if skill_name in items:
                            continue
                            
                        skill_path = os.path.join(root, skill_name, "SKILL.md")
                        if os.path.exists(skill_path):
                            desc = "No description available."
                            try:
                                with open(skill_path, "r", encoding="utf-8") as f:
                                    content = f.read()
                                    if content.startswith("---"):
                                        parts = content.split("---", 2)
                                        if len(parts) >= 3:
                                            frontmatter = yaml.safe_load(parts[1])
                                            desc = frontmatter.get("description", desc)
                            except:
                                pass
                            items[skill_name] = (skill_name, desc)
            ui.display_list("skills", list(items.values()), columns=["Name", "Description"])
        elif args.list == "agents":
            items = []
            for name, cfg in engine.agent_classes.items():
                desc = cfg.description
                if not desc and cfg.system_prompt:
                    desc = cfg.system_prompt.split('\n')[0].strip()
                items.append((name, cfg.code, cfg.model, desc))
            ui.display_list("agents", items, columns=["Name", "Code", "Model", "Description"])
        elif args.list == "tools":
            items = [(t["name"], t.get("description", "No description.")) for t in engine.tool_schemas]
            ui.display_list("tools", items, columns=["Name", "Description"])
        elif args.list == "sessions":
            ui.display_list("sessions", engine.get_sessions_list(), columns=["Session ID", "Task"])
        return

    # Interpret the requested agent class for the RUN.  YAHAEngine maps a short
    # code to its class, so `-a Cha` and `-a ChatAgent` are the same class:
    # resolve the request to the canonical name, and refuse an unknown class
    # BEFORE the run so a raw `-a` value can never silently select a different
    # class.  Only a real run reaches this point; `--list` and `--compile`
    # returned above.
    requested_class = args.agent or default_agent_class(engine)
    resolved_config = engine.resolve_agent_class(requested_class)
    if resolved_config is None:
        print(f"Unknown agent class: {requested_class}")
        sys.exit(1)
    canonical_name = getattr(resolved_config, "name", None)
    # A stubbed engine (unit tests patch YAHAEngine) yields a non-str name;
    # fall back to the request then, so the run/error path is unchanged there.
    agent_class = (
        canonical_name
        if isinstance(canonical_name, str) and canonical_name
        else requested_class
    )

    if not prompt:
        print("Error: No task prompt provided.")
        parser.print_help()
        sys.exit(1)

    result = engine.run_agent(agent_class, prompt)
    
    if args.output and result:
        try:
            with open(args.output, "w", encoding="utf-8") as out_f:
                out_f.write(result)
        except Exception as e:
            print(f"Warning: Could not save output to {args.output}: {e}")
    
    # Commit any changes in memory or ext folders
    commit_memory_changes(".", prompt)
    commit_ext_changes(".", prompt)
    
    # Check for automatic sleep trigger
    check_and_trigger_sleep(".", engine)
    
    if not result or result.startswith("Error"):
        if result:
            print(f"\n{result}")
        print("Task execution failed.")

    print(engine.get_token_usage_report())

if __name__ == "__main__":
    try:
        main()
    except MarkdownLoadError as e:
        print(f"Error: {e}")
        sys.exit(1)
