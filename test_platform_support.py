import tempfile
import unittest
from pathlib import Path

from platform_support import (
    detached_popen_options,
    is_matlab_process_name,
    platform_profile,
    select_recovery_candidate,
)


class PlatformProfileTests(unittest.TestCase):
    def test_current_and_intel_mac_targets(self):
        self.assertTrue(platform_profile("Windows", "AMD64")["adapter_supported"])
        self.assertTrue(platform_profile("Linux", "x86_64")["current_matlab_official"])
        self.assertEqual(platform_profile("Darwin", "arm64")["target"], "macos-apple-silicon")
        intel = platform_profile("Darwin", "x86_64")
        self.assertTrue(intel["adapter_supported"])
        self.assertTrue(intel["compatibility_mode"])

    def test_matlab_process_names_do_not_include_helpers(self):
        self.assertTrue(is_matlab_process_name("MATLAB"))
        self.assertTrue(is_matlab_process_name("matlab.exe"))
        self.assertTrue(is_matlab_process_name("MATLAB_R2025b"))
        self.assertFalse(is_matlab_process_name("MATLABWebUI.exe"))

    def test_detach_options_match_operating_system(self):
        self.assertIn("creationflags", detached_popen_options("Windows"))
        self.assertTrue(detached_popen_options("Linux")["start_new_session"])
        self.assertTrue(detached_popen_options("Darwin")["start_new_session"])

    def test_architecture_binary_is_rewritten_to_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "bin" / "matlab"
            binary = root / "bin" / "maca64" / "MATLAB"
            launcher.parent.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            launcher.touch()
            binary.touch()
            selected = select_recovery_candidate(
                [
                    {
                        "pid": 20,
                        "ppid": 1,
                        "argv": [str(binary), "-batch", "run_job"],
                        "cwd": str(root),
                        "executable": str(binary),
                    }
                ],
                {20},
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["argv"][0], str(launcher))


if __name__ == "__main__":
    unittest.main()
