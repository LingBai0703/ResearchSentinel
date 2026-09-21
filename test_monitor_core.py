import tempfile
import time
import unittest
from pathlib import Path

from monitor_core import Artifact, MonitorState, choose_active_job, parse_csv_progress, parse_progress_lines


class ProgressParsingTests(unittest.TestCase):
    def test_parses_project_progress_and_eta(self):
        parsed = parse_progress_lines(
            [
                "Resumed 23/280 communication-power points.",
                "trial=4/40 L_FA=7 time=38.5s completed=24/280",
                "trial=4/40 L_FA=8 time=39.5s completed=25/280",
            ]
        )
        self.assertEqual((parsed.completed, parsed.total), (25, 280))
        self.assertEqual((parsed.trial, parsed.trials), (4, 40))
        self.assertEqual(parsed.item_seconds, 39.0)
        self.assertEqual(parsed.eta_seconds, 255 * 39.0)

    def test_unit_ok_does_not_mark_incomplete_job_complete(self):
        parsed = parse_progress_lines(
            [
                "COMM_POWER_UNIT_OK analytic=(0.2,0.05)",
                "trial=1/40 time=36.4s completed=1/280",
            ]
        )
        self.assertFalse(parsed.success)
        self.assertFalse(parsed.complete)

    def test_csv_progress_uses_filename_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job_40channels_7points_progress.csv"
            path.write_text("L_FA,Completed\n5,5\n6,5\n7,4\n", encoding="utf-8")
            parsed = parse_csv_progress(path)
        self.assertEqual(parsed.completed, 14)
        self.assertEqual(parsed.total, 280)

    def test_new_resumed_log_beats_old_completed_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "formal.stdout.log"
            new = root / "resume.stdout.log"
            old.write_text("completed=280/280\nFORMAL_OK\n", encoding="utf-8")
            new.write_text("Resumed 23/280\ncompleted=31/280\n", encoding="utf-8")
            now = time.time()
            old.touch()
            new.touch()
            artifacts = [
                Artifact(old, now - 60, old.stat().st_size),
                Artifact(new, now, new.stat().st_size),
            ]
            job = choose_active_job(artifacts, now - 600)
        self.assertEqual((job.progress.completed, job.progress.total), (31, 280))


class StateTests(unittest.TestCase):
    def test_missing_process_after_observation_is_crash(self):
        state = MonitorState(60)
        state.had_process = True
        health, _ = state.classify(time.time(), [], choose_active_job([], None))
        self.assertEqual(health, "CRASHED")


if __name__ == "__main__":
    unittest.main()
