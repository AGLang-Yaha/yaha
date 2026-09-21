# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import yaha.cli as cli


class TestCLIOutputParameter(unittest.TestCase):
    def setUp(self):
        # Create a temporary workspace directory
        self.workspace = tempfile.mkdtemp(prefix="yaha_cli_test_")
        
        # Setup required .yaha config folder structure so YAHAEngine doesn't crash on init
        self.yaha_dir = os.path.join(self.workspace, ".yaha")
        os.makedirs(os.path.join(self.yaha_dir, "memory"))
        os.makedirs(os.path.join(self.yaha_dir, "ext"))
        os.makedirs(os.path.join(self.yaha_dir, "logs"))
        
        # Write dummy/minimal files
        with open(os.path.join(self.yaha_dir, "tool-schema.md"), "w", encoding="utf-8") as f:
            f.write("[]")
        with open(os.path.join(self.yaha_dir, "AgentClass.md"), "w", encoding="utf-8") as f:
            f.write("## BaseAgent\n- **Code:** Baa\n- **Model:** DefaultModel\n- **System Prompt:** > Worker prompt\n### Toolset\n")
        with open(os.path.join(self.yaha_dir, "Model.md"), "w", encoding="utf-8") as f:
            f.write("")
            
        # Store original directory to restore on tearDown
        self.original_cwd = os.getcwd()

    def tearDown(self):
        # Restore directory and clean up temp workspace
        os.chdir(self.original_cwd)
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("yaha.cli.YAHAEngine")
    @patch("sys.stdin.isatty", return_value=True)
    def test_cli_output_saves_result_to_file(self, mock_isatty, mock_engine_class):
        # Setup mock YAHAEngine and Agent run return
        mock_engine = MagicMock()
        mock_engine_class.return_value = mock_engine
        mock_engine.run_agent.return_value = "Expected Agent Response Text"
        mock_engine.session.session_id = "test_session_id"
        mock_engine.session.folder_name = "test_session_folder"
        mock_engine.get_token_usage_report.return_value = "Tokens: 0"
        
        # Temporary output file path
        out_file_path = os.path.join(self.workspace, "out.txt")
        
        # Mock sys.argv to run cli with -o and workspace args
        test_argv = [
            "run-yaha",
            "-w", self.workspace,
            "-a", "BaseAgent",
            "-o", out_file_path,
            "Do a task"
        ]
        
        with patch("sys.argv", test_argv):
            cli.main()
            
        # Verify the output file was created and contains the expected result
        self.assertTrue(os.path.exists(out_file_path))
        with open(out_file_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "Expected Agent Response Text")


if __name__ == "__main__":
    unittest.main()
