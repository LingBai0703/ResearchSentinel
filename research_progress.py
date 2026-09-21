"""Optional progress reporting from an already-running Python research script."""
import json
import os
from pathlib import Path

import psutil

from app_paths import data_dir


def report(project, completed, total, state="running", message="", data_path=None):
    """Use state='completed' only after saving final results; 'failed' on failure."""
    if state not in {"running", "completed", "failed"} or not 0 <= completed <= total or total <= 0:
        raise ValueError("Invalid progress")
    base = data_path or os.environ.get("RESEARCH_SENTINEL_DATA_DIR") or data_dir()
    directory = Path(base) / "progress"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{os.getpid()}.json"
    temporary = target.with_suffix(".tmp")
    value = {"started": psutil.Process().create_time(), "completed": completed,
             "total": total, "state": state, "message": message}
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, target)
