"""Small, bounded task used only by the browser integration test."""
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_progress import report

root = Path(sys.argv[1])
for step in range(10):
    report(root, step, 10, message=f"Fixture step {step}")
    time.sleep(1)
(root / "plot.png").write_bytes(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
report(root, 10, 10, state="completed", message="Fixture finished")
