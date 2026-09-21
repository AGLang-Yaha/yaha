# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import re
import platform
import tempfile

class AccessController:
    def __init__(self, workspace_root, user_home=None, yes_for_all=False):
        self.workspace_root = os.path.realpath(workspace_root)
        self.user_home = user_home or os.path.expanduser("~")
        self.fs_whitelist_path = os.path.join(self.user_home, ".yaha", "security", "fs_whitelist.md")
        self.fs_whitelist = self._load_fs_whitelist()
        self.yes_for_all = yes_for_all
        
        # bash_tool Whitelist (Tier 1)
        self.bash_whitelist = {
            "ls", "grep", "find", "cat", "git status", "git diff", "pwd", "date", "echo", "head", "tail", "wc"
        }
        
        # bash_tool Danger Patterns (Tier 2)
        self.danger_patterns = [
            r"\bsudo\b",
            r"\brm\s+-rf\b",
            r"\bchmod\b",
            r"\bchown\b",
            r">\s*/dev/",
            r"\.ssh\b",
            r"\.env\b",
            r"\bnc\b",
            r"\bnmap\b",
            r"\bcurl\b",
            r"\bwget\b"
        ]

    def _load_fs_whitelist(self):
        """Loads allowed external paths from ~/.yaha/security/fs_whitelist.md"""
        whitelist = set()
        if os.path.exists(self.fs_whitelist_path):
            try:
                with open(self.fs_whitelist_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            whitelist.add(os.path.realpath(line))
            except Exception as e:
                print(f"Warning: Could not load FS whitelist: {e}")
        return whitelist

    def validate_path(self, path, mode='read'):
        """
        Validates if a path is accessible to an agent.
        Returns (is_allowed, error_message)
        """
        try:
            abs_path = os.path.realpath(path)
            
            # Global Block Tier 0 (Protection outside or inside workspace)
            forbidden_patterns = [r"\.git\b", r"\.env\b", r"\.ssh\b", r"\.DS_Store\b"]
            for pattern in forbidden_patterns:
                if re.search(pattern, abs_path):
                    pattern_name = pattern.strip(r"\b")
                    return False, f"Access Denied: '{pattern_name}' is a restricted system pattern."

            # Check if it's within the workspace root
            if abs_path.startswith(self.workspace_root):
                
                # Check for sensitive areas within workspace
                rel_path = os.path.relpath(abs_path, self.workspace_root)
                
                # Granular .yaha protection
                if rel_path == ".yaha" or rel_path.startswith(".yaha" + os.sep):
                    # PUBLIC areas (Read/Write)
                    public_dirs = [
                        os.path.join(".yaha", "memory"),
                        os.path.join(".yaha", "logs"),
                        os.path.join(".yaha", "ext")
                    ]
                    for p in public_dirs:
                        if rel_path == p or rel_path.startswith(p + os.sep):
                            return True, ""
                    
                    # Core Config Files (Read-Only)
                    core_configs = ["AgentClass.md", "Model.md", "tool-schema.md", "layout.md", "SOUL.md"]
                    if any(rel_path == os.path.join(".yaha", cfg) for cfg in core_configs):
                        if mode == 'read':
                            return True, ""
                        else:
                            return False, f"Access Denied: '{rel_path}' is a read-only core configuration file."
                    
                    return False, "Access Denied: YAHA core configuration files are private."
                
                # Symlink target check (ensure it doesn't point outside workspace)
                if os.path.islink(path):
                    link_target = os.path.realpath(os.readlink(path))
                    if not link_target.startswith(self.workspace_root):
                        # Check whitelist for symlinks
                        if not any(link_target.startswith(w) for w in self.fs_whitelist):
                            return False, "Access Denied: Symlink points outside the workspace root and is not whitelisted."
                
                return True, ""

            # Check if it's in the global whitelist
            for allowed in self.fs_whitelist:
                if abs_path == allowed or abs_path.startswith(allowed + os.sep):
                    return True, ""

            return False, f"Access Denied: Path '{path}' is outside the authorized workspace root."
        except Exception as e:
            return False, f"Error validating path: {str(e)}"

    def authorize_bash(self, agent_id, cmdline, ui=None):
        """
        Authorizes a bash command based on the three-tier model.
        Returns (is_authorized, message)
        """
        if self.yes_for_all:
            return True, "Yes-for-All Granted"

        cmd_stripped = cmdline.strip()
        
        # Tier 1: Whitelist (Silent)
        # Support single-token commands and exact multi-word commands (e.g., "git status").
        # Whitelist match must be exact or a prefix followed by a space; the remainder
        # is checked for command-chaining characters to prevent bypasses like "ls; rm".
        for allowed in self.bash_whitelist:
            if cmd_stripped == allowed or cmd_stripped.startswith(allowed + " "):
                remainder = cmd_stripped[len(allowed):]
                if not re.search(r'[;&|]', remainder):
                    return True, "Whitelisted"

        # Tier 2: Security Checker (Heuristics)
        for pattern in self.danger_patterns:
            if re.search(pattern, cmd_stripped, re.IGNORECASE):
                # Unsafe command, must prompt user
                if ui:
                    msg = f"Agent {agent_id} wants to run an UNSAFE command:\n\n```bash\n{cmdline}\n```"
                    ans = ui.prompt_user("Security", "ACS", f"{msg}\n\nAllow execution? (y/n/A)", default="n").lower()
                    if ans in ['y', 'a', 'all']:
                        if ans in ['a', 'all']:
                            self.yes_for_all = True
                        return True, "User Approved (Unsafe)"
                return False, f"Security Block: Command '{cmdline}' is flagged as dangerous."

        # Tier 3: User Confirmation (Fallback for unknown commands)
        if ui:
            msg = f"Agent {agent_id} wants to run:\n\n```bash\n{cmdline}\n```"
            ans = ui.prompt_user("Security", "ACS", f"{msg}\n\nAllow execution? (y/n/A)", default="n").lower()
            if ans in ['y', 'a', 'all']:
                if ans in ['a', 'all']:
                    self.yes_for_all = True
                return True, "User Approved"
        
        return False, "Access Denied: Command requires user approval."

    def get_sandbox_command(self, cmdline):
        """
        Wraps the command line with MacOS sandbox-exec if on Darwin.
        """
        if platform.system() != "Darwin":
            return cmdline
            
        yaha_dir = os.path.join(self.workspace_root, ".yaha")
        memory_dir = os.path.join(yaha_dir, "memory")
        ext_dir = os.path.join(yaha_dir, "ext")

        # sandbox-exec profile (scheme format)
        # Use (allow default) so /bin/bash and common utilities can run,
        # then apply targeted denies to keep writes inside the workspace and
        # protect the .yaha core configuration area.
        profile = f"""(version 1)
(allow default)
(deny file-write* (subpath "/"))
(allow file-write* (subpath "{self.workspace_root}"))
"""
        # Toolchains legitimately write to the process temp dir (python
        # tempfile, pytest tmp_path, compiler/git scratch). Allow writes to the
        # per-user temp dir only; on macOS tempfile.gettempdir() is $TMPDIR
        # (/var/folders/.../T, realpath'd), not the shared /tmp.
        tmpdir = os.path.realpath(tempfile.gettempdir())
        if tmpdir and tmpdir != self.workspace_root:
            profile += f'(allow file-write* (subpath "{tmpdir}"))\n'

        # Add whitelisted paths for writing
        for allowed in self.fs_whitelist:
            profile += f'(allow file-write* (subpath "{allowed}"))\n'

        # Block .yaha and other sensitive patterns, then re-open only the
        # two agent-owned areas, memory/ and ext/.
        #
        # IMPORTANT - how sandbox-exec actually evaluates this profile
        # (established empirically on macOS 15): rules are grouped by
        # operation class. For a write op, ONLY file-write* rules are
        # consulted (last match wins); file* rules are ignored entirely when
        # any file-write* rule matches. Same for read ops with file-read*
        # rules. file* rules only decide ops that no class-specific rule
        # matches (and possibly auxiliary ops). Consequences:
        #  1. The blanket workspace write allow above would make ALL of
        #     .yaha writable via pure-write ops (e.g. 'echo x >> f'), since
        #     the generic 'deny file* .yaha' never gets consulted for write
        #     ops. Hence the explicit file-write* deny on .yaha right below,
        #     with the memory/ext write allows AFTER it (class-internal
        #     last-match-wins).
        #  2. Read ops on .yaha hit no file-read* rule (except memory/ext),
        #     so they fall back to the file* rules - where the ws-.git allow
        #     regex at the end of the profile would re-open .git paths under
        #     .yaha. Hence the trailing yaha-.git deny regex, plus the
        #     memory/.git and ext/.git file* allows after it for the two
        #     intended exceptions.
        profile += f"""(deny file-write* (subpath "{yaha_dir}"))
(deny file* (subpath "{yaha_dir}"))
(deny file* (regex #"/\\.git(/|$)"))
(deny file* (regex #"/\\.ssh(/|$)"))
(deny file* (regex #"/\\.env(/|$)"))
(allow file-read* (subpath "{memory_dir}"))
(allow file-write* (subpath "{memory_dir}"))
(allow file-read* (subpath "{ext_dir}"))
(allow file-write* (subpath "{ext_dir}"))
(allow file-read-data (literal "/dev/null"))
(allow file-write-data (literal "/dev/null"))
(allow file-read-data (literal "/dev/zero"))
(allow file-write-data (literal "/dev/zero"))
(allow file-read-data (literal "/dev/urandom"))
(allow file-read-data (literal "/dev/random"))
(allow file-read* (literal "/dev/tty"))
"""
        # Git repositories INSIDE the workspace - any <workspace>/<sub>/.git,
        # including the workspace root repo - are fully open to bash: agents
        # drive them through git porcelain (and the prompts tell them to
        # commit memory changes). Within the file* class SBPL is
        # last-match-wins, so this allow re-opens exactly those .git paths
        # after the blanket deny above, and the deny right after keeps .git
        # paths under .yaha closed (write ops on them are already denied by
        # the file-write* deny on .yaha above); memory/.git and ext/.git -
        # agent-owned repos created by --init - are the two explicit
        # exceptions. .git OUTSIDE the workspace stays denied, and the file
        # tools never touch .git at all (validate_path Tier 0).
        ws_git = re.escape(self.workspace_root)
        profile += f'(allow file* (regex #"^{ws_git}/(.*/)?\\.git(/|$)"))\n'
        yaha_git = re.escape(yaha_dir)
        profile += f'(deny file* (regex #"^{yaha_git}/(.*/)?\\.git(/|$)"))\n'
        profile += f'(allow file* (subpath "{os.path.join(memory_dir, ".git")}"))\n'
        profile += f'(allow file* (subpath "{os.path.join(ext_dir, ".git")}"))\n'

        # Escape single quotes in profile for the CLI string
        safe_profile = profile.replace("'", "'\\''")
        
        # We wrap in /bin/bash -c to handle complex shell syntax within the sandbox
        # Escape double quotes in cmdline for the bash -c string
        # We also need to escape $ and ` to prevent early evaluation by the outer shell
        safe_cmd = cmdline.replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
        
        return f"sandbox-exec -p '{safe_profile}' /bin/bash -c \"{safe_cmd}\""
