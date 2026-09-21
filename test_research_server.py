import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from monitor_core import JobInfo, MonitorSnapshot, ProgressInfo
from research_server import DashboardHandler, ResearchSupervisor, discover_results, is_within


class RecoverySchedulingTests(unittest.TestCase):
    def make_supervisor(self, root: Path) -> ResearchSupervisor:
        supervisor = ResearchSupervisor(root, [root], 2, 10, 48, root / "data")
        supervisor.recovery_command = {
            "argv": [str(root / "matlab.exe"), "-batch", "run_job"],
            "cwd": str(root),
            "job_directory": str(root),
        }
        return supervisor

    def test_auto_restart_is_scheduled_with_initial_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            now = time.time()
            supervisor._schedule_restart(now, "test")
            self.assertAlmostEqual(supervisor.next_restart_at, now + 10, places=3)

    def test_disabled_auto_restart_does_not_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            supervisor.settings["auto_restart"] = False
            supervisor._schedule_restart(time.time(), "test")
            self.assertIsNone(supervisor.next_restart_at)

    def test_recovery_replays_captured_command_into_new_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "matlab.exe"
            executable.touch()
            supervisor = self.make_supervisor(root)
            supervisor.recovery_command["argv"][0] = str(executable)
            supervisor.snapshot = MonitorSnapshot(
                timestamp=time.time(),
                processes=[],
                job=JobInfo(
                    directory=root,
                    progress=ProgressInfo(completed=3, total=10),
                ),
                health="CRASHED",
                message="test",
            )
            with patch("research_server.subprocess.Popen") as popen:
                ok, _ = supervisor._launch_recovery()
            self.assertTrue(ok)
            self.assertEqual(supervisor.restart_attempts, 1)
            self.assertEqual(popen.call_args.args[0], [str(executable), "-batch", "run_job"])
            self.assertTrue(any(root.glob("monitor_restart_*.stdout.log")))
            self.assertTrue(any(root.glob("monitor_restart_*.stderr.log")))


class ResultDiscoveryTests(unittest.TestCase):
    def test_only_current_task_results_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / "results"
            job.mkdir()
            old = job / "old.png"
            current = job / "current.png"
            old.write_bytes(b"old")
            current.write_bytes(b"current")
            now = time.time()
            old_time = now - 7200
            old.touch()
            current.touch()
            import os

            os.utime(old, (old_time, old_time))
            snapshot = MonitorSnapshot(
                timestamp=now,
                processes=[],
                job=JobInfo(directory=job, progress=ProgressInfo(completed=10, total=10)),
                health="COMPLETED",
                message="done",
            )
            results = discover_results(root, snapshot, since=now - 600)
        self.assertEqual([item["name"] for item in results], ["current.png"])

    def test_artifact_boundary_rejects_sibling_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            sibling = Path(directory) / "other" / "plot.png"
            root.mkdir()
            sibling.parent.mkdir()
            sibling.touch()
            self.assertFalse(is_within(sibling, root))


class DashboardHandlerTests(unittest.TestCase):
    def test_client_disconnect_does_not_escape_handler(self):
        handler = object.__new__(DashboardHandler)
        with patch.object(BaseHTTPRequestHandler, "handle", side_effect=ConnectionResetError):
            handler.handle()


if __name__ == "__main__":
    unittest.main()
