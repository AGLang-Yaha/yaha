#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

"""Persistent python kernel: state, failure, timeout, death, isolation.

The kernel is a real subprocess, one per agent, so these tests spawn it. They
assert the properties the hook design depends on:

* a later block sees the variables an earlier block assigned;
* an exception is a failure, and the namespace is NOT rolled back;
* ``print`` is captured per block and cannot corrupt the protocol;
* ``emit()`` is an explicit capture channel;
* a hung block is killed by the timeout and restarted EMPTY;
* an unexpected death is detected and the next block gets a fresh kernel;
* two kernels share no state and are both reaped on close.
"""
import os
import shutil
import tempfile
import unittest

from yaha.hooks.kernel import PythonKernel


class _KernelCase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="yaha_kernel_")

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def kernel(self, timeout=300.0):
        kern = PythonKernel(workspace=self.workspace, timeout=timeout)
        self.addCleanup(kern.close)
        return kern


class StatePersistence(_KernelCase, unittest.TestCase):
    def test_variables_persist_across_blocks(self):
        kern = self.kernel()
        first = kern.run("x = 41")
        self.assertTrue(first.ok, first.note)
        second = kern.run("print(x + 1)")
        self.assertTrue(second.ok)
        self.assertEqual(second.stdout.strip(), "42")

    def test_print_is_captured_per_block(self):
        kern = self.kernel()
        kern.run("print('one')")
        second = kern.run("print('two')")
        self.assertEqual(second.stdout.strip(), "two")

    def test_emit_is_an_explicit_channel(self):
        kern = self.kernel()
        result = kern.run("emit('structured')\nprint('plain')")
        self.assertTrue(result.ok)
        self.assertEqual(result.emitted, ["structured"])
        self.assertEqual(result.stdout.strip(), "plain")

    def test_stderr_is_captured(self):
        kern = self.kernel()
        result = kern.run("import sys\nsys.stderr.write('warn\\n')")
        self.assertTrue(result.ok)
        self.assertEqual(result.stderr.strip(), "warn")

    def test_runs_in_the_workspace_cwd(self):
        kern = self.kernel()
        result = kern.run("import os\nprint(os.getcwd())")
        self.assertTrue(result.ok)
        self.assertEqual(os.path.realpath(result.stdout.strip()),
                         os.path.realpath(self.workspace))


class FailureSemantics(_KernelCase, unittest.TestCase):
    def test_exception_is_a_failure_with_a_traceback(self):
        kern = self.kernel()
        result = kern.run("raise ValueError('boom')")
        self.assertFalse(result.ok)
        self.assertEqual(result.exc_type, "ValueError")
        self.assertEqual(result.exc_value, "boom")
        self.assertIn("ValueError", result.traceback or "")

    def test_namespace_is_not_rolled_back_on_exception(self):
        kern = self.kernel()
        failed = kern.run("kept = 'value'\nraise RuntimeError('later')")
        self.assertFalse(failed.ok)
        after = kern.run("print(kept)")
        self.assertTrue(after.ok)
        self.assertEqual(after.stdout.strip(), "value")

    def test_systemexit_does_not_kill_the_kernel(self):
        kern = self.kernel()
        result = kern.run("import sys\nsys.exit(3)")
        self.assertFalse(result.ok)
        self.assertEqual(result.exc_type, "SystemExit")
        self.assertTrue(kern.run("print('alive')").ok)

    def test_yaha_dom_is_bound_from_the_snapshot(self):
        kern = self.kernel()
        snapshot = {"agent": {"name": "CrewCda",
                              "agent_class": {"__t": "class", "short_name": "Cwd",
                                              "lineage": ["BaseAgent", "CodeAgent", "CrewCda"]}},
                    "stage": {"name": "Coding"},
                    "tool": None}
        result = kern.run(
            "print(yaha.agent.agent_class.short_name)\n"
            "print(yaha.agent.agent_class.inherits('CodeAgent'))\n"
            "print(yaha.agent.agent_class.inherits('NoAgent'))\n"
            "print(yaha.stage.name)\n"
            "print(yaha.tool)", snapshot)
        self.assertTrue(result.ok, result.exc_value)
        lines = [line for line in result.stdout.strip().split("\n")]
        self.assertEqual(lines[0], "Cwd")
        self.assertEqual(lines[1], "True")
        self.assertEqual(lines[2], "False")
        self.assertEqual(lines[3], "Coding")
        self.assertEqual(lines[4], "None")

    def test_dom_is_read_only(self):
        kern = self.kernel()
        snapshot = {"agent": {"name": "CrewCda"}}
        result = kern.run("yaha.agent.name = 'hacked'", snapshot)
        self.assertFalse(result.ok)
        self.assertEqual(result.exc_type, "YahaReadOnlyError")
        result = kern.run("yaha.brand_new = 1", snapshot)
        self.assertFalse(result.ok)
        self.assertEqual(result.exc_type, "YahaReadOnlyError")

    def test_unknown_dom_attribute_is_an_attribute_error(self):
        kern = self.kernel()
        result = kern.run("print(yaha.nope)", {"agent": {"name": "x"}})
        self.assertFalse(result.ok)
        self.assertEqual(result.exc_type, "AttributeError")


class TimeoutAndRestart(_KernelCase, unittest.TestCase):
    def test_timeout_kills_and_empties_the_namespace(self):
        kern = self.kernel(timeout=1.0)
        kern.run("survivor = 1")
        self.assertTrue(kern.run("print(survivor)").ok)
        timed_out = kern.run("while True:\n    pass")
        self.assertFalse(timed_out.ok)
        self.assertIn("timed out", (timed_out.note or ""))
        self.assertGreaterEqual(kern.resets, 1)
        # the kernel was replaced by an EMPTY one
        after = kern.run("print('survivor' in dir())")
        self.assertTrue(after.ok)
        self.assertEqual(after.stdout.strip(), "False")

    def test_unexpected_death_is_detected_and_recovered(self):
        kern = self.kernel()
        kern.run("import os\nos._exit(9)")
        after = kern.run("print('fresh')")
        self.assertTrue(after.ok)
        self.assertEqual(after.stdout.strip(), "fresh")
        self.assertGreaterEqual(kern.resets, 1)

    def test_stray_stdout_cannot_corrupt_the_protocol(self):
        kern = self.kernel()
        result = kern.run("import os\nos.write(1, b'garbage not a record\\n')\n"
                          "print('still fine')")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "still fine")

    def test_close_reaps_the_process(self):
        kern = self.kernel()
        self.assertTrue(kern.run("print(1)").ok)
        pid = kern.pid
        self.assertIsNotNone(pid)
        kern.close()
        self.assertFalse(kern.running)
        self.assertIsNone(kern.pid)
        # the pid must no longer be signalable
        with self.assertRaises(OSError):
            os.kill(pid, 0)


class Isolation(_KernelCase, unittest.TestCase):
    def test_two_kernels_share_no_state(self):
        one = self.kernel()
        two = self.kernel()
        one.run("shared = 'one'")
        result = two.run("print('shared' in dir())")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "False")
        self.assertNotEqual(one.pid, two.pid)

    def test_view_reports_pid_running_and_resets(self):
        kern = self.kernel()
        view = kern.view()
        self.assertFalse(view["running"])
        self.assertIsNone(view["pid"])
        kern.run("print(1)")
        view = kern.view()
        self.assertTrue(view["running"])
        self.assertEqual(view["resets"], 0)
        self.assertIsInstance(view["pid"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
