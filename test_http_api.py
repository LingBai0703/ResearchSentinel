import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from monitor_core import JobInfo, MonitorSnapshot
from research_server import DashboardServer, ResearchSupervisor


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.supervisor = ResearchSupervisor(root, [root], 2, 10, 48, root / "data")
        self.supervisor.snapshot = MonitorSnapshot(time.time(), [], JobInfo(), "WAITING", "")
        self.server = DashboardServer(("127.0.0.1", 0), self.supervisor, Path(__file__).parent / "web")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def post(self, route, payload, origin=None, content_type="application/json"):
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + route, json.dumps(payload).encode(), headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_status_and_settings_roundtrip(self):
        code, _ = self.post("/api/settings", {"retention_hours": 24, "refresh_seconds": 3})
        self.assertEqual(code, 200)
        with urlopen(self.base + "/api/status", timeout=3) as response:
            data = json.load(response)
        self.assertEqual(data["settings"]["refresh_seconds"], 3)
        self.assertIn("tasks", data)
        self.assertIn("memory_protection", data)

    def test_cross_origin_mutation_rejected(self):
        code, _ = self.post("/api/settings", {"lan_enabled": True}, "http://untrusted.invalid")
        self.assertEqual(code, 403)

    def test_non_json_mutation_rejected(self):
        code, _ = self.post("/api/settings", {}, content_type="text/plain")
        self.assertEqual(code, 415)

    def test_invalid_setting_leaves_existing_value(self):
        code, _ = self.post("/api/settings", {"refresh_seconds": -1})
        self.assertEqual(code, 400)
        self.assertEqual(self.supervisor.settings["refresh_seconds"], 2)

    def test_delete_unknown_task_is_controlled_error(self):
        code, _ = self.post("/api/task/delete", {"id": "missing", "stop": False})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
