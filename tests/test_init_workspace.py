import os
import shutil
import tempfile
import unittest

from yaha.cli import init_workspace


class TestInitWorkspace(unittest.TestCase):
    """Smoke test: --init must complete end-to-end (regression: the
    whole-folder template copy refactor dropped the agent_path variable
    and crashed with NameError right after the ext agent was written)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yaha_init_test_")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_init_completes_and_links_ext_agent(self):
        init_workspace("TestAgent")
        yaha_dir = os.path.join(self.dir, ".yaha")
        # core template files shipped
        agentclass = os.path.join(yaha_dir, "AgentClass.md")
        self.assertTrue(os.path.exists(agentclass))
        # workspace-specific ext agent created and included
        safe_name = "".join(c if c.isalnum() else "_"
                            for c in os.path.basename(self.dir))
        ext_agent = os.path.join(yaha_dir, "ext", f"{safe_name}.md")
        self.assertTrue(os.path.exists(ext_agent))
        with open(agentclass, "r", encoding="utf-8") as f:
            self.assertIn(f'<!-- include path="ext/{safe_name}.md" -->', f.read())
        # git repos initialised for the agent-owned areas
        for sub in ("memory", "ext", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(yaha_dir, sub, ".git")), sub)


if __name__ == "__main__":
    unittest.main()
