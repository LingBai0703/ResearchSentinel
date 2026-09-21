import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_server import DEFAULT_SETTINGS, atomic_json_write, discover_results, snapshot_dict
from service_settings import autostart_spec, validate_settings
from task_store import TaskStore, identity, python_command


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "data"
        self.state.mkdir()
        self.settings = dict(DEFAULT_SETTINGS)
        self.store = self.load()
        self.memory = patch("task_store.psutil.virtual_memory",
                            return_value=SimpleNamespace(percent=40, available=8 * 1024**3))
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def load(self):
        return TaskStore(self.root, self.state, self.settings, atomic_json_write,
                         discover_results, snapshot_dict)

    def row(self, pid=100, started=1000):
        return {"pid": pid, "ppid": 0, "started": started, "identity": identity(pid, started),
                "name": "python", "kind": "python", "argv": [sys.executable, "experiment.py"],
                "executable": sys.executable, "cwd": str(self.root),
                "cpu_percent": 1, "memory_bytes": 4096, "status": "running"}

    def create(self):
        self.store.poll(rows={100: self.row()}, now=1010)
        return next(iter(self.store.tasks.values()))

    def report(self, state, completed=2, total=10):
        directory = self.state / "progress"
        directory.mkdir(exist_ok=True)
        (directory / "100.json").write_text(json.dumps(
            {"started": 1000, "completed": completed, "total": total, "state": state}))

    def test_default_restart_and_independent_switch(self):
        self.store.poll(rows={100: self.row(), 101: self.row(101)}, now=1010)
        tasks = list(self.store.tasks.values())
        self.assertTrue(all(t["auto_restart"] for t in tasks))
        self.store.configure(tasks[0]["id"], {"auto_restart": False})
        self.assertFalse(tasks[0]["auto_restart"])
        self.assertTrue(tasks[1]["auto_restart"])
        self.assertFalse(self.load().tasks[tasks[0]["id"]]["auto_restart"])

    def test_python_tooling_modules_are_not_research_tasks(self):
        for module in ("pip", "pytest", "unittest", "PyInstaller", "compileall"):
            self.assertFalse(python_command([sys.executable, "-m", module]))
        self.assertTrue(python_command([sys.executable, "-m", "my_experiment", "--run"]))

    def test_unobserved_exit_not_restarted(self):
        task = self.create()
        self.store.poll(rows={}, now=1020)
        self.assertEqual(task["status"], "EXITED")
        self.assertIsNone(task["next_restart"])

    def test_completed_record_survives_exit_and_reload(self):
        task = self.create()
        self.report("completed", 10)
        self.store.poll(rows={}, now=1020)
        self.assertEqual(task["status"], "COMPLETED")
        self.store.save()
        self.assertEqual(self.load().tasks[task["id"]]["status"], "COMPLETED")
        self.store.poll(rows={}, now=1020 + 24 * 3600 - 1)
        self.assertIn(task["id"], self.store.tasks)
        self.store.poll(rows={}, now=1020 + 24 * 3600 + 1)
        self.assertNotIn(task["id"], self.store.tasks)

    def test_incomplete_exit_schedules_only_this_task(self):
        task = self.create()
        self.report("running")
        self.store.poll(rows={}, now=1020)
        self.assertEqual(task["status"], "CRASHED")
        self.assertEqual(task["next_restart"], 1030)

    def test_memory_latch_cancels_all_and_survives_restart(self):
        task = self.create()
        task["next_restart"] = 2000
        self.store.check_memory(SimpleNamespace(percent=95, available=1024**3))
        self.assertFalse(task["auto_restart"])
        self.assertIsNone(task["next_restart"])
        self.assertTrue(self.load().memory_latched)
        self.store.poll(rows={100: self.row(), 101: self.row(101)}, now=1020)
        self.assertTrue(all(not t["auto_restart"] for t in self.store.tasks.values()))

    def test_low_available_memory_triggers_even_with_low_percent(self):
        self.create()
        self.assertTrue(self.store.check_memory(SimpleNamespace(percent=40, available=100 * 1024**2)))

    def test_memory_blocks_reenable(self):
        task = self.create()
        with patch("task_store.psutil.virtual_memory", return_value=SimpleNamespace(percent=96, available=1024**3)):
            with self.assertRaises(ValueError):
                self.store.configure(task["id"], {"auto_restart": True})

    def test_delete_does_not_terminate_and_is_not_rediscovered(self):
        task = self.create()
        with patch("task_store.psutil.Process") as process:
            self.store.delete(task["id"])
            process.assert_not_called()
        self.store = self.load()
        self.store.poll(rows={100: self.row()}, now=1020)
        self.assertEqual(self.store.tasks, {})

    def test_stop_deletion_checks_create_time(self):
        task = self.create()
        p = Mock(pid=100)
        p.create_time.return_value = 2000
        with patch("task_store.psutil.Process", return_value=p):
            self.store.delete(task["id"], True)
        p.terminate.assert_not_called()

    def test_stop_deletion_terminates_matching_identity(self):
        task = self.create()
        p = Mock(pid=100)
        p.create_time.return_value = 1000
        with patch("task_store.psutil.Process", return_value=p), patch("task_store.psutil.wait_procs", return_value=([], [])):
            self.store.delete(task["id"], True)
        p.terminate.assert_called_once()

    def test_unresponsive_process_keeps_task_and_disables_recovery(self):
        task = self.create()
        p = Mock(pid=100)
        p.create_time.return_value = 1000
        with patch("task_store.psutil.Process", return_value=p), patch("task_store.psutil.wait_procs", return_value=([], [p])):
            with self.assertRaises(ValueError):
                self.store.delete(task["id"], True)
        self.assertIn(task["id"], self.store.tasks)
        self.assertFalse(task["auto_restart"])

    def test_new_process_reusing_deleted_pid_is_visible(self):
        task = self.create()
        self.store.delete(task["id"])
        self.store.poll(rows={100: self.row(started=2000)}, now=2010)
        self.assertEqual(len(self.store.tasks), 1)

    def test_python_parent_workers_are_one_task(self):
        row = self.row(101)
        row["ppid"] = 100
        self.store.poll(rows={100: self.row(), 101: row}, now=1010)
        self.assertEqual(len(self.store.tasks), 1)
        self.assertEqual(len(next(iter(self.store.tasks.values()))["processes"]), 2)

    def test_stale_progress_file_ignored(self):
        task = self.create()
        self.report("completed", 10)
        task["identities"] = [identity(100, 2000)]
        self.store.update_progress(task, 2010)
        self.assertFalse(task["job"]["progress"]["complete"])

    def test_paths_cannot_escape_project(self):
        task = self.create()
        with self.assertRaises(ValueError):
            self.store.configure(task["id"], {"output_dir": str(self.root.parent)})
        with self.assertRaises(ValueError):
            self.store.configure(task["id"], {"auto_restart": False, "output_dir": str(self.root.parent)})
        self.assertTrue(task["auto_restart"])

    def test_history_keeps_last_process_metrics(self):
        task = self.create()
        self.store.poll(rows={}, now=1020)
        self.assertEqual(task["processes"], [])
        self.assertEqual(task["last_processes"][0]["pid"], 100)
        self.assertEqual(task["last_processes"][0]["memory_bytes"], 4096)

    def test_recovery_failure_is_bounded(self):
        task = self.create()
        self.report("running")
        self.store.poll(rows={}, now=1020)
        with patch("task_store.subprocess.Popen", side_effect=OSError("fixture failure")) as popen:
            for tick in range(1, 14):
                self.store.poll(rows={}, now=1020 + tick * 400)
        self.assertEqual(task["attempts"], self.settings["max_restart_attempts"])
        self.assertEqual(popen.call_count, self.settings["max_restart_attempts"])

    def test_explicit_log_and_results_are_task_local(self):
        self.store.poll(rows={100: self.row(), 101: self.row(101)}, now=1010)
        a, b = list(self.store.tasks.values())
        directory = self.root / "results"
        directory.mkdir()
        (directory / "plot.png").write_bytes(b"png")
        log = directory / "run.stdout.log"
        log.write_text("completed=10/10\n")
        self.store.configure(a["id"], {"output_dir": str(directory), "log_path": str(log)})
        self.store.update_progress(a, 1020)
        self.assertEqual(len(a["results"]), 1)
        self.assertEqual(b["results"], [])

    def test_recovery_records_child_identity(self):
        task = self.create()
        task["processes"] = []
        child = Mock(pid=222)
        p = Mock()
        p.create_time.return_value = 1020
        with patch("task_store.subprocess.Popen", return_value=child) as popen, patch("task_store.psutil.Process", return_value=p):
            self.store.restart(task, 1020)
        self.assertEqual(popen.call_args.args[0], task["argv"])
        self.assertIn(identity(222, 1020), task["identities"])
        self.assertEqual(task["attempts"], 1)


class SettingsTests(unittest.TestCase):
    def test_validation_rejects_bad_values(self):
        for payload in ({"retention_hours": 0}, {"lan_enabled": "yes"}, {"memory_limit_percent": 100},
                        {"refresh_seconds": 0.5}, {"memory_min_mb": True}):
            with self.assertRaises(ValueError):
                validate_settings(payload, DEFAULT_SETTINGS)

    def test_autostart_windows_mac_and_linux_specs(self):
        import plistlib
        for system in ("windows", "macos", "linux"):
            path, content, control = autostart_spec(Path("/research project"), 8765, system, "/home/example")
            self.assertIn("8765", content.decode("utf-16" if system == "windows" else "utf-8"))
            if system == "macos":
                self.assertIn("--project", plistlib.loads(content)["ProgramArguments"])
            if system == "linux":
                self.assertEqual(control, ["systemctl", "--user"])
                self.assertIn("default.target", content.decode())

    def test_python_recognition(self):
        self.assertTrue(python_command(["python3.12", "run.py"]))
        self.assertTrue(python_command(["python.exe", "-m", "experiment"]))
        self.assertFalse(python_command(["python", "-c", "print(1)"]))
        self.assertFalse(python_command(["python", "-m", "ipykernel_launcher"]))
        self.assertFalse(python_command(["python"]))


if __name__ == "__main__":
    unittest.main()
