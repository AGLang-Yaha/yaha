import os
import platform
import shutil
import tempfile
import unittest

from yaha.tools.system_tools import bash_tool
from yaha.core.access import AccessController


class TestAccessControllerSandbox(unittest.TestCase):
    """Tests for AccessController bash sandboxing. Run on macOS only."""

    def setUp(self):
        # Create the workspace under the project root instead of /var/folders;
        # sandbox-exec on macOS applies extra restrictions to /var/folders that
        # can cause false negatives in these tests.
        self.base_dir = os.path.join(os.path.dirname(__file__), "..", "test_tmp")
        os.makedirs(self.base_dir, exist_ok=True)
        self.workspace = tempfile.mkdtemp(prefix="yaha_test_", dir=self.base_dir)
        yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(yaha_dir, "memory"))
        os.makedirs(os.path.join(yaha_dir, "ext"))
        os.makedirs(os.path.join(yaha_dir, "logs"))
        with open(os.path.join(yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.ac = AccessController(workspace_root=self.workspace, yes_for_all=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_sandbox_runs(self):
        result = bash_tool("echo hello_sandbox", access_controller=self.ac, agent_id="test")
        self.assertEqual(result.strip(), "hello_sandbox")

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_blocks_writes_outside_workspace(self):
        result = bash_tool(
            "touch /tmp/yaha_test_outside_should_fail 2>&1 || echo BLOCKED",
            access_controller=self.ac,
            agent_id="test",
        )
        self.assertTrue("BLOCKED" in result or "Operation not permitted" in result)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_blocks_core_yaha(self):
        core_file = os.path.join(self.workspace, ".yaha", "tool-schema.md")
        result = bash_tool(
            f"touch {core_file} 2>&1 || echo BLOCKED",
            access_controller=self.ac,
            agent_id="test",
        )
        self.assertTrue("BLOCKED" in result or "Operation not permitted" in result)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_allows_memory_dir(self):
        test_file = os.path.join(self.workspace, ".yaha", "memory", "sandbox_test_ok")
        try:
            result = bash_tool(
                f"touch {test_file} && echo OK",
                access_controller=self.ac,
                agent_id="test",
            )
            self.assertIn("OK", result)
            self.assertTrue(os.path.exists(test_file))
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_allows_tempdir_writes(self):
        # Toolchains (python tempfile, pytest tmp_path, git) need write access
        # to the per-user temp dir even though it is outside the workspace.
        probe = os.path.join(tempfile.gettempdir(), "yaha_sandbox_tmpdir_probe")
        try:
            result = bash_tool(
                f"touch {probe} && echo OK",
                access_controller=self.ac,
                agent_id="test",
            )
            self.assertIn("OK", result)
            self.assertTrue(os.path.exists(probe))
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_git_status_does_not_fatal_on_dev_null(self):
        # Regression test for: fatal: could not open '/dev/null' for reading and writing
        import subprocess

        subprocess.run(["git", "init"], cwd=self.workspace, capture_output=True, check=True)
        # bash_tool runs in the process cwd, not the workspace; the sandbox
        # denies every .git except the workspace one, so git must be pointed
        # at the workspace explicitly (yes_for_all skips Tier-1, which only
        # whitelists bare "git status" as the first token).
        self.ac.yes_for_all = True
        result = bash_tool(
            f"cd {self.workspace} && git status --short",
            access_controller=self.ac,
            agent_id="test",
        )
        self.assertNotIn("fatal", result.lower())
        self.assertNotIn("could not open '/dev/null'", result)

    @unittest.skipUnless(platform.system() == "Darwin", "sandbox-exec is macOS only")
    def test_bash_tool_git_dir_access_matrix(self):
        # .git opening rules for bash: memory/.git, ext/.git, the workspace
        # root repo and any <workspace>/<sub>/.git are open; .git under
        # .yaha (other than memory/ext) and .git outside the workspace stay
        # denied, as does the rest of .yaha - including via pure-write ops
        # (file-write* rules are evaluated separately from file* rules).
        import subprocess

        def git_repo(path):
            os.makedirs(path, exist_ok=True)
            subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)

        ws = self.workspace
        for d in [os.path.join(".yaha", "memory"), os.path.join(".yaha", "ext"),
                  os.path.join(".yaha", "logs"), "sub1"]:
            git_repo(os.path.join(ws, d))
        git_repo(ws)
        outside = tempfile.mkdtemp(prefix="yaha_outside_", dir=self.base_dir)
        self.addCleanup(shutil.rmtree, outside, True)
        git_repo(outside)

        def probe(cmd):
            return bash_tool(f"{cmd} 2>&1 || echo BLOCKED",
                             access_controller=self.ac, agent_id="test")

        # open: git porcelain on root, memory, ext and subdir repos
        for d in [ws, os.path.join(ws, ".yaha", "memory"),
                  os.path.join(ws, ".yaha", "ext"), os.path.join(ws, "sub1")]:
            self.assertNotIn("BLOCKED", probe(f"git -C {d} status --short"), d)
        # open: writes to open .git dirs
        for d in [ws, os.path.join(ws, "sub1")]:
            self.assertNotIn("BLOCKED", probe(f"echo x > {d}/.git/probe && rm {d}/.git/probe"), d)
        # denied: .git under .yaha (logs) and outside the workspace
        self.assertIn("BLOCKED", probe(f"git -C {os.path.join(ws, '.yaha', 'logs')} status --short"))
        self.assertIn("BLOCKED", probe(f"git -C {outside} status --short"))
        self.assertIn("BLOCKED", probe(f"echo x > {os.path.join(ws, '.yaha', 'logs', '.git', 'hook')}"))
        self.assertIn("BLOCKED", probe(f"echo x > {os.path.join(outside, '.git', 'hook')}"))
        # denied: pure-write op on .yaha core (no read involved, so this only
        # passes if the write-class deny on .yaha is in place)
        self.assertIn("BLOCKED", probe(f"echo x >> {os.path.join(ws, '.yaha', 'tool-schema.md')}"))


class TestAccessControllerAuthorization(unittest.TestCase):
    """Cross-platform tests for command authorization tiers."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_test_")
        self.ac = AccessController(workspace_root=self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_authorize_bash_whitelist_silent(self):
        authorized, msg = self.ac.authorize_bash("test", "ls -la")
        self.assertTrue(authorized)
        self.assertEqual(msg, "Whitelisted")

    def test_authorize_bash_danger_pattern_blocked_without_ui(self):
        authorized, msg = self.ac.authorize_bash("test", "rm -rf /tmp/foo")
        self.assertFalse(authorized)
        self.assertIn("Security Block", msg)


if __name__ == "__main__":
    unittest.main()
