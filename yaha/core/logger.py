# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import re
from datetime import datetime

class AgentLogger:
    def __init__(self, session, agent_class, agent_id, caller_id=None, task=None):
        self.session = session
        self.agent_class = agent_class
        self.agent_id = agent_id
        self.caller_id = caller_id
        self.task = task
        self.log_path = session.get_log_path(agent_class, agent_id)
        self._last_logged_turn = None
        self._logged_count = 0
        self.session.ensure_log_dir()
        self._initialize_log()

    def set_task(self, task):
        self.task = task
        self._initialize_log()

    def _format_reasoning(self, content):
        if not isinstance(content, str):
            return content
        # Replace <reasoning>...</reasoning> with collapsible HTML details/summary fold
        pattern = r"<reasoning>(.*?)</reasoning>"
        replacement = r"<details>\n<summary>🧠 Thought Process (Reasoning)</summary>\n\n\1\n</details>"
        return re.sub(pattern, replacement, content, flags=re.DOTALL | re.IGNORECASE)

    def _register_log_in_index(self):
        index_path = os.path.join(self.session.workspace_dir, ".yaha", "Logs.md")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # In Obsidian, the path relative to vault root should be used for links.
        # Since the vault is usually opened at the workspace root:
        rel_log_path = os.path.join(".yaha", "logs", self.session.folder_name, f"{self.agent_class}-{self.agent_id}.md")
        
        link_title = f"{self.agent_class} ({self.agent_id}) - {time_str}"
        task_snippet = ""
        if self.task:
            task_snippet = self.task.strip().split('\n')[0]
            if len(task_snippet) > 60:
                task_snippet = task_snippet[:60] + "..."
            task_snippet = f" | `{task_snippet}`"
            
        index_line = f"- [[{rel_log_path}|{link_title}]]{task_snippet}\n"
        
        file_existed = os.path.exists(index_path)
        with open(index_path, "a", encoding="utf-8") as f:
            if not file_existed:
                f.write("# YAHA Session Execution Logs Index\n\n")
            f.write(index_line)

    def _initialize_log(self):
        content = "---\n"
        content += f"agent_class: {self.agent_class}\n"
        content += f"agent_id: {self.agent_id}\n"
        if self.caller_id:
            content += f"caller: {self.caller_id}\n"
        if self.task:
            # Ensure task is on a single line for front matter or escaped
            task_clean = self.task.replace('\n', ' ')
            content += f"task: {task_clean}\n"
        content += "---\n\n"
        
        log_existed = os.path.exists(self.log_path)
        
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        if not log_existed:
            try:
                self._register_log_in_index()
            except Exception:
                pass

    def log_turn(self, turn_number, message_history):
        """Flushes only the new messages added to history in this turn."""
        if not hasattr(self, '_logged_count'):
            self._logged_count = 0
        
        new_msgs = message_history[self._logged_count:]
        
        with open(self.log_path, "a", encoding="utf-8") as f:
            if new_msgs:
                # Write turn header ONLY ONCE per turn to keep logs clean
                if self._last_logged_turn != turn_number:
                    f.write(f"## Turn {turn_number}\n\n")
                    self._last_logged_turn = turn_number
                    
                for msg in new_msgs:
                    f.write(f"### {msg['role'].capitalize()}\n\n")
                    f.write(f"{self._format_reasoning(msg['content'])}\n\n")
                    f.write("---\n\n")
        
        self._logged_count = len(message_history)

    def log_compression(self, note):
        """Appends a compression marker to the log. Full original content stays intact."""
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("## Context Compressed\n\n")
            f.write(f"{note}\n\n")
            f.write("---\n\n")
        self._last_logged_turn = None

    def log_compression_restart(self, message_history, note):
        """Appends a compression marker plus the rebuilt (post-restart) history,
        then re-syncs incremental logging to the new history length."""
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("## Context Compressed\n\n")
            f.write(f"{note}\n\n")
            f.write("---\n\n")
            for msg in message_history:
                f.write(f"### {msg['role'].capitalize()}\n\n")
                f.write(f"{self._format_reasoning(msg['content'])}\n\n")
                f.write("---\n\n")
        self._logged_count = len(message_history)
        self._last_logged_turn = None

    def log_full_history(self, message_history):
        """Rewrites the log with the full history."""
        self._initialize_log()
        with open(self.log_path, "a", encoding="utf-8") as f:
            turn_counter = 1
            for msg in message_history:
                if msg["role"] == "system":
                    continue
                # Reconstruct turn header only on real user prompts (exclude tool results)
                is_real_user_turn = (
                    msg["role"] == "user" and 
                    not msg["content"].startswith("TOOL RESULT (") and 
                    not msg["content"].startswith("COMPRESSED TOOL RESULT")
                )
                if is_real_user_turn:
                    f.write(f"## Turn {turn_counter}\n\n")
                    turn_counter += 1
                f.write(f"### {msg['role'].capitalize()}\n\n")
                f.write(f"{self._format_reasoning(msg['content'])}\n\n")
                f.write("---\n\n")
        
        # Reset logged counters to keep incremental appending working correctly
        self._logged_count = len(message_history)
        self._last_logged_turn = None

    # --- silent recovered-error channel -----------------------------------

    ERROR_LOG_MESSAGE_LIMIT = 2000

    def _format_error_field(self, value):
        """Render one error-log field: single line, never empty, no raw separators.

        Whitespace runs (including newlines) collapse to a single space, and the
        literal field delimiter ``|`` is backslash-escaped, so a multi-line
        provider payload can neither break the one-line-per-entry rule nor forge
        an extra ``key=value`` field on the line.
        """
        text = str(value if value is not None else '-')
        text = ' '.join(text.split())
        text = text.replace('|', '\\|')
        return text if text else '-'

    def log_error(self, category, message, stage=None, turn=None, attempt=None, extra=None):
        """Append one recovered-error entry to the session error log.

        Silent by design: nothing is printed and NO failure here may propagate -
        a logging problem must never be able to break a run. The file is
        append-only with one fixed-format line per entry so it stays greppable:

            <timestamp> | error | category=.. | agent=.. | stage=.. | turn=.. | attempt=.. | msg=..

        One open+write per entry keeps interleaving between concurrent
        sub-agents in the same session to whole lines.
        """
        try:
            get_path = getattr(self.session, 'get_error_log_path', None)
            if get_path is None:
                return
            path = get_path()
            ensure_dir = getattr(self.session, 'ensure_error_log_dir', None)
            if ensure_dir is not None:
                ensure_dir()

            parts = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'error',
                'category=' + self._format_error_field(category),
                'agent=' + self._format_error_field(
                    f'{self.agent_class}({self.agent_id})'
                ),
                'stage=' + self._format_error_field(stage),
                'turn=' + self._format_error_field(turn),
                'attempt=' + self._format_error_field(attempt),
            ]
            if extra:
                try:
                    for key, value in dict(extra).items():
                        parts.append(
                            self._format_error_field(key) + '=' + self._format_error_field(value)
                        )
                except Exception:
                    parts.append('extra=' + self._format_error_field(extra))

            body = self._format_error_field(message)
            if len(body) > self.ERROR_LOG_MESSAGE_LIMIT:
                body = body[:self.ERROR_LOG_MESSAGE_LIMIT] + ' [truncated]'
            parts.append('msg=' + body)

            with open(path, 'a', encoding='utf-8') as f:
                f.write(' | '.join(parts) + '\n')
        except Exception:
            # Logging must never break the run.
            return
