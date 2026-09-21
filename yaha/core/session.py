import uuid
from datetime import datetime
import os

class Session:
    def __init__(self, workspace_dir=".", session_id=None, extra_skill_dirs=None):
        self.workspace_dir = workspace_dir
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.extra_skill_dirs = extra_skill_dirs or []
        self.timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.folder_name = f"{self.timestamp}-{self.session_id}"
        self.log_dir = os.path.join(".yaha", "logs", self.folder_name)

    def ensure_log_dir(self):
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)

    def get_skill_roots(self):
        """Returns the prioritized list of skill search directories."""
        roots = []
        
        # 1. Additional folders (explicitly specified)
        if self.extra_skill_dirs:
            roots.extend(self.extra_skill_dirs)
            
        # 2. Workspace configuration folder
        roots.append(os.path.join(self.workspace_dir, ".yaha", "skills"))
        
        # 3. User configuration folder
        user_home = os.path.expanduser("~")
        roots.append(os.path.join(user_home, ".yaha", "skills"))
        
        return roots

    def get_log_path(self, agent_class, agent_id):
        filename = f"{agent_class}-{agent_id}.md"
        return os.path.join(self.log_dir, filename)

    def get_error_log_path(self):
        """Absolute path of the silent recovered-error log for this session.

        Anchored to ``workspace_dir`` on purpose: ``log_dir`` above is a relative
        path, so turn logs resolve against the process CWD and can land outside
        the workspace. The error channel must be deterministic (and testable).
        """
        return os.path.join(
            self.workspace_dir, ".yaha", "logs", self.folder_name, "error.log"
        )

    def ensure_error_log_dir(self):
        """Create the directory that holds the session error log."""
        os.makedirs(os.path.dirname(self.get_error_log_path()), exist_ok=True)
