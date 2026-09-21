#!/usr/bin/env python3
"""Web supervisor for already-started MATLAB and Python research jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import shutil
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse

import psutil
from app_paths import bundled_web_dir, data_dir as default_data_dir, runtime_command
from service_settings import autostart_status, set_autostart, validate_settings
from task_store import TaskStore

from monitor_core import (
    JobInfo,
    MonitorSnapshot,
    MonitorState,
    ProcessSampler,
    append_event,
    default_watch_roots,
    make_snapshot,
    save_crash_report,
)
from platform_support import (
    detached_popen_options,
    is_matlab_process_name,
    is_matlab_executable,
    platform_profile,
    select_recovery_candidate,
)


DEFAULT_SETTINGS = {
    "auto_restart": True,
    "restart_delay_seconds": 10,
    "max_restart_attempts": 5,
    "refresh_seconds": 2,
    "retention_hours": 24,
    "memory_limit_percent": 92,
    "memory_min_mb": 512,
    "autostart": False,
}
PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
RESULT_EXTENSIONS = PREVIEW_EXTENSIONS | {".pdf", ".fig", ".eps"}


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def migrate_legacy_data(legacy_dir: Path, target_dir: Path) -> None:
    """Move the old project-local state into the program-local data folder once."""
    if not legacy_dir.is_dir() or (target_dir / "tasks.json").exists():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in legacy_dir.iterdir():
        destination = target_dir / item.name
        if destination.exists():
            continue
        try:
            shutil.move(str(item), str(destination))
        except OSError:
            # A stale or partially removed legacy file must not prevent startup.
            continue
    try:
        legacy_dir.rmdir()
    except OSError:
        pass


def migrate_legacy_progress(project_root: Path, target_dir: Path) -> None:
    legacy_dir = project_root / ".research-progress"
    progress_dir = target_dir / "progress"
    if not legacy_dir.is_dir() or progress_dir.exists():
        return
    try:
        shutil.move(str(legacy_dir), str(progress_dir))
    except OSError:
        pass


def iso_time(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def process_dict(process: Any, now: float) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "name": process.name,
        "cpu_percent": round(process.cpu_percent, 1),
        "memory_bytes": process.memory_bytes,
        "started_at": iso_time(process.started),
        "uptime_seconds": max(0, now - process.started),
        "status": process.status,
        "executable": process.executable,
    }


def snapshot_dict(snapshot: MonitorSnapshot) -> dict[str, Any]:
    progress = snapshot.job.progress
    return {
        "timestamp": iso_time(snapshot.timestamp),
        "timestamp_epoch": snapshot.timestamp,
        "health": snapshot.health,
        "message": snapshot.message,
        "processes": [process_dict(item, snapshot.timestamp) for item in snapshot.processes],
        "totals": {
            "cpu_percent": round(sum(item.cpu_percent for item in snapshot.processes), 1),
            "memory_bytes": sum(item.memory_bytes for item in snapshot.processes),
        },
        "job": {
            "directory": str(snapshot.job.directory) if snapshot.job.directory else None,
            "latest_activity": iso_time(snapshot.job.latest_activity),
            "activity_age_seconds": (
                max(0, snapshot.timestamp - snapshot.job.latest_activity)
                if snapshot.job.latest_activity
                else None
            ),
            "progress": {
                "completed": progress.completed,
                "total": progress.total,
                "fraction": progress.fraction,
                "trial": progress.trial,
                "trials": progress.trials,
                "eta_seconds": progress.eta_seconds,
                "item_seconds": progress.item_seconds,
                "source": str(progress.source) if progress.source else None,
                "latest_line": progress.latest_line,
                "complete": progress.complete,
            },
            "checkpoint": (
                {
                    "path": str(snapshot.job.checkpoint.path),
                    "size": snapshot.job.checkpoint.size,
                    "modified": iso_time(snapshot.job.checkpoint.modified),
                    "age_seconds": max(0, snapshot.timestamp - snapshot.job.checkpoint.modified),
                }
                if snapshot.job.checkpoint
                else None
            ),
            "stderr": (
                {"path": str(snapshot.job.stderr.path), "size": snapshot.job.stderr.size}
                if snapshot.job.stderr
                else None
            ),
            "log_tail": progress.tail,
            "stderr_tail": snapshot.job.stderr_tail,
        },
    }


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def discover_results(
    project_root: Path,
    snapshot: MonitorSnapshot,
    since: Optional[float] = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    directory = snapshot.job.directory
    if not directory or not directory.is_dir() or not is_within(directory, project_root):
        return []
    found: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in {"tmp", ".git", "__pycache__"}]
        for name in files:
            path = Path(current) / name
            extension = path.suffix.lower()
            if extension not in RESULT_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since - 300:
                continue
            found.append(
                {
                    "name": path.name,
                    "path": str(path.resolve()),
                    "relative_path": str(path.resolve().relative_to(project_root.resolve())),
                    "modified": iso_time(stat.st_mtime),
                    "modified_epoch": stat.st_mtime,
                    "size": stat.st_size,
                    "previewable": extension in PREVIEW_EXTENSIONS,
                    "extension": extension,
                }
            )
    found.sort(key=lambda item: item["modified_epoch"], reverse=True)
    return found[:limit]


class ResearchSupervisor:
    def __init__(
        self,
        project_root: Path,
        watch_roots: Sequence[Path],
        interval: float,
        stall_minutes: float,
        lookback_hours: float,
        data_dir: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root
        self.watch_roots = list(watch_roots)
        self.interval = interval
        self.lookback_hours = lookback_hours
        # Keep direct Python imports backwards compatible for tests and older
        # integrations; the CLI always passes the program-local data folder.
        self.state_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.state_dir / "settings.json"
        self.command_path = self.state_dir / "recovery_command.json"
        self.server_events_path = self.state_dir / "server_events.jsonl"
        self.settings = {**DEFAULT_SETTINGS, **load_json(self.settings_path, {})}
        self.recovery_command: Optional[dict[str, Any]] = load_json(self.command_path, None)
        self.sampler = ProcessSampler()
        self.monitor_state = MonitorState(stall_minutes * 60)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.snapshot: Optional[MonitorSnapshot] = None
        self.history: deque[dict[str, Any]] = deque(maxlen=300)
        self.events: deque[dict[str, Any]] = deque(maxlen=80)
        self.last_health: Optional[str] = None
        self.last_progress: Optional[int] = None
        self.restart_attempts = 0
        self.restart_progress_baseline: Optional[int] = None
        self.next_restart_at: Optional[float] = None
        self.launch_deadline: Optional[float] = None
        self.last_restart_log: Optional[str] = None
        self.last_recovery_message = ""
        self.crash_saved_for_outage = False
        self.initial_state_checked = False
        self.active_task_started: Optional[float] = None
        self.tasks = TaskStore(project_root, self.state_dir, self.settings, atomic_json_write,
                               discover_results, snapshot_dict)
        self.port = 8765
        self.rebind = None
        self.thread = threading.Thread(target=self._loop, name="research-supervisor", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        self.thread.join(timeout=5)
        with self.lock:
            self.tasks.save()

    def rescan(self) -> None:
        self.wake_event.set()

    def set_auto_restart(self, enabled: bool) -> None:
        with self.lock:
            self.settings["auto_restart"] = bool(enabled)
            if not enabled:
                self.next_restart_at = None
                self.launch_deadline = None
                self.last_recovery_message = "自动恢复已关闭"
            atomic_json_write(self.settings_path, self.settings)
            self._record_event("setting", "自动恢复已开启" if enabled else "自动恢复已关闭")
        self.wake_event.set()

    def _record_event(self, kind: str, message: str, **extra: Any) -> None:
        event = {"time": iso_time(time.time()), "kind": kind, "message": message, **extra}
        self.events.appendleft(event)
        with self.server_events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _capture_recovery_command(self, snapshot: MonitorSnapshot) -> None:
        if not snapshot.processes:
            return
        pids = {item.pid for item in snapshot.processes}
        candidates: list[dict[str, Any]] = []
        for process in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "cwd"]):
            try:
                info = process.info
                if int(info.get("pid") or -1) not in pids:
                    continue
                if not is_matlab_process_name(str(info.get("name") or "")):
                    continue
                argv = [str(value) for value in (info.get("cmdline") or []) if value is not None]
                executable = str(info.get("exe") or (argv[0] if argv else ""))
                if argv and executable:
                    candidates.append(
                        {
                            "pid": int(info["pid"]),
                            "ppid": int(info.get("ppid") or 0),
                            "argv": argv,
                            "cwd": str(info.get("cwd") or self.project_root),
                            "executable": executable,
                        }
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        selected = select_recovery_candidate(candidates, pids)
        if not selected:
            return
        command = {
            "captured_at": iso_time(time.time()),
            "source_pid": selected["pid"],
            "argv": selected["argv"],
            "cwd": selected["cwd"],
            "job_directory": str(snapshot.job.directory) if snapshot.job.directory else None,
        }
        identity = (command["argv"], command["cwd"], command["job_directory"])
        old_identity = None
        if self.recovery_command:
            old_identity = (
                self.recovery_command.get("argv"),
                self.recovery_command.get("cwd"),
                self.recovery_command.get("job_directory"),
            )
        if identity != old_identity:
            self.recovery_command = command
            atomic_json_write(self.command_path, command)
            self._record_event("command_captured", "已捕获当前 MATLAB 恢复命令")

    def _schedule_restart(self, now: float, reason: str) -> None:
        if not self.settings.get("auto_restart", True) or self.next_restart_at:
            return
        if not self.recovery_command:
            self.last_recovery_message = "未捕获到可恢复的 MATLAB 启动命令"
            return
        maximum = int(self.settings.get("max_restart_attempts", 5))
        if self.restart_attempts >= maximum:
            self.last_recovery_message = f"自动恢复已达到上限（{maximum} 次）"
            self._record_event("recovery_limit", self.last_recovery_message)
            return
        delay = min(float(self.settings.get("restart_delay_seconds", 10)) * (2 ** self.restart_attempts), 300.0)
        self.next_restart_at = now + delay
        self.last_recovery_message = f"{reason}；{int(delay)} 秒后自动恢复"
        self._record_event("recovery_scheduled", self.last_recovery_message)

    def _launch_recovery(self, manual: bool = False) -> tuple[bool, str]:
        with self.lock:
            snapshot = self.snapshot
            if snapshot and snapshot.processes:
                return False, "MATLAB 仍在运行，未执行重复启动"
            if not self.recovery_command:
                return False, "没有捕获到可恢复的 MATLAB 启动命令"
            if snapshot and snapshot.job.progress.complete:
                return False, "任务已有完成标记，无需恢复"
            command = dict(self.recovery_command)
            argv = command.get("argv")
            cwd = Path(command.get("cwd") or self.project_root)
            if not isinstance(argv, list) or not argv:
                return False, "恢复命令无效"
            executable = Path(str(argv[0]))
            if not is_matlab_executable(executable) or not executable.exists():
                return False, "恢复命令不是有效的 MATLAB 启动器"
            if not cwd.is_dir():
                cwd = self.project_root
            output_dir_value = command.get("job_directory")
            output_dir = Path(output_dir_value) if output_dir_value else self.state_dir / "restarts"
            if not output_dir.is_dir():
                output_dir = self.state_dir / "restarts"
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            stdout_path = output_dir / f"monitor_restart_{stamp}.stdout.log"
            stderr_path = output_dir / f"monitor_restart_{stamp}.stderr.log"
            try:
                with stdout_path.open("ab", buffering=0) as stdout_handle, stderr_path.open("ab", buffering=0) as stderr_handle:
                    subprocess.Popen(
                        [str(value) for value in argv],
                        cwd=str(cwd),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        **detached_popen_options(),
                    )
            except OSError as error:
                message = f"恢复启动失败：{error}"
                self.last_recovery_message = message
                self._record_event("recovery_failed", message)
                return False, message
            self.restart_attempts += 1
            self.restart_progress_baseline = snapshot.job.progress.completed if snapshot else self.last_progress
            self.next_restart_at = None
            self.launch_deadline = time.time() + 30
            self.last_restart_log = str(stdout_path)
            mode = "手动" if manual else "自动"
            message = f"{mode}恢复已启动（第 {self.restart_attempts} 次）"
            self.last_recovery_message = message
            self._record_event("recovery_started", message, stdout=str(stdout_path), stderr=str(stderr_path))
            return True, message

    def manual_restart(self) -> tuple[bool, str]:
        result = self._launch_recovery(manual=True)
        self.wake_event.set()
        return result

    def _legacy_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                snapshot = make_snapshot(self.sampler, self.monitor_state, self.watch_roots, self.lookback_hours)
                now = snapshot.timestamp
                with self.lock:
                    previous = self.snapshot
                    previous_running = bool(previous and previous.processes)
                    running = bool(snapshot.processes)
                    if running:
                        process_started = min(item.started for item in snapshot.processes)
                        if self.active_task_started is None or not previous_running:
                            self.active_task_started = process_started
                        self._capture_recovery_command(snapshot)
                        self.next_restart_at = None
                        self.launch_deadline = None
                        self.crash_saved_for_outage = False
                        completed = snapshot.job.progress.completed
                        if completed is not None and self.restart_progress_baseline is not None and completed > self.restart_progress_baseline:
                            self.restart_attempts = 0
                            self.restart_progress_baseline = None
                            self.last_recovery_message = "恢复后进度已继续"
                            self._record_event("recovery_confirmed", self.last_recovery_message)
                    elif previous_running and not snapshot.job.progress.complete:
                        if not self.crash_saved_for_outage:
                            report_snapshot = MonitorSnapshot(
                                timestamp=now,
                                processes=previous.processes,
                                job=snapshot.job,
                                health="CRASHED",
                                message="MATLAB 在任务完成前消失",
                            )
                            report = save_crash_report(self.state_dir, report_snapshot)
                            append_event(self.state_dir, snapshot, report)
                            self._record_event("unexpected_exit", "MATLAB 在任务完成前退出", report=str(report))
                            self.crash_saved_for_outage = True
                        self._schedule_restart(now, "检测到未完成任务异常退出")
                    elif (
                        not running
                        and not self.initial_state_checked
                        and self.recovery_command
                        and snapshot.job.progress.completed is not None
                        and snapshot.job.progress.total is not None
                        and not snapshot.job.progress.complete
                    ):
                        self._schedule_restart(now, "发现此前已启动但尚未完成的任务")
                    elif not running and self.launch_deadline and now >= self.launch_deadline:
                        self.launch_deadline = None
                        self._schedule_restart(now, "恢复进程未能稳定启动")
                    if not running and self.launch_deadline and now < self.launch_deadline:
                        snapshot.health = "RESTARTING"
                        snapshot.message = self.last_recovery_message
                    elif not running and self.next_restart_at:
                        snapshot.health = "RECOVERY_PENDING"
                        snapshot.message = self.last_recovery_message
                    if snapshot.health != self.last_health:
                        append_event(self.state_dir, snapshot)
                        self._record_event("health", snapshot.message, health=snapshot.health)
                        self.last_health = snapshot.health
                    if snapshot.job.progress.completed is not None:
                        self.last_progress = snapshot.job.progress.completed
                    self.initial_state_checked = True
                    self.snapshot = snapshot
                    self.history.append(
                        {
                            "time": now,
                            "cpu": round(sum(item.cpu_percent for item in snapshot.processes), 1),
                            "memory": sum(item.memory_bytes for item in snapshot.processes),
                            "progress": snapshot.job.progress.fraction,
                        }
                    )
                    due = bool(not running and self.next_restart_at and now >= self.next_restart_at)
                if due:
                    self._launch_recovery(manual=False)
            except Exception as error:
                with self.lock:
                    self._record_event("monitor_error", f"监控循环异常：{error}")
            self.wake_event.wait(self.interval)
            self.wake_event.clear()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                snapshot = make_snapshot(self.sampler, self.monitor_state, self.watch_roots, self.lookback_hours)
                with self.lock:
                    self.snapshot = snapshot
                    self.tasks.import_completed(snapshot, self.recovery_command)
                    self.tasks.poll(snapshot)
                    processes = [p for task in self.tasks.tasks.values() for p in task["processes"]]
                    self.history.append({"time": time.time(), "cpu": sum(p["cpu_percent"] for p in processes),
                                         "memory": sum(p["memory_bytes"] for p in processes),
                                         "progress": snapshot.job.progress.fraction})
            except Exception as error:
                with self.lock:
                    self._record_event("monitor_error", str(error))
            self.wake_event.wait(self.settings.get("refresh_seconds", self.interval))
            self.wake_event.clear()

    def update_settings(self, payload):
        with self.lock:
            updated = validate_settings(payload, self.settings)
            if updated.get("autostart") != self.settings.get("autostart"):
                set_autostart(self.project_root, self.port, updated["autostart"])
            network_changed = updated.get("lan_enabled") != self.settings.get("lan_enabled")
            self.settings.clear()
            self.settings.update(updated)
            atomic_json_write(self.settings_path, self.settings)
            self.tasks.check_memory()
        self.wake_event.set()
        return network_changed

    def status(self) -> dict[str, Any]:
        with self.lock:
            if self.snapshot is None:
                return {"ready": False, "message": "监控器正在初始化"}
            data = snapshot_dict(self.snapshot)
            command = self.recovery_command or {}
            argv = command.get("argv") or []
            data.update(
                {
                    "ready": True,
                    "project_root": str(self.project_root),
                    "platform": platform_profile(),
                    "results": [],
                    "settings": dict(self.settings),
                    "recovery": {
                        "command_available": bool(self.recovery_command),
                        "captured_at": command.get("captured_at"),
                        "cwd": command.get("cwd"),
                        "command_preview": " ".join(str(value) for value in argv)[:260],
                        "attempts": self.restart_attempts,
                        "max_attempts": int(self.settings.get("max_restart_attempts", 5)),
                        "next_restart_at": iso_time(self.next_restart_at),
                        "next_restart_epoch": self.next_restart_at,
                        "launch_deadline": iso_time(self.launch_deadline),
                        "last_message": self.last_recovery_message,
                        "last_restart_log": self.last_restart_log,
                    },
                    "history": list(self.history),
                    "events": list(self.events)[:30],
                }
            )
            tasks = list(self.tasks.tasks.values())
            tasks.sort(key=lambda t: t["started"], reverse=True)
            processes = [p for task in tasks for p in task["processes"]]
            data["tasks"] = json.loads(json.dumps(tasks))
            data["processes"] = processes
            data["totals"] = {"cpu_percent": sum(p["cpu_percent"] for p in processes),
                              "memory_bytes": sum(p["memory_bytes"] for p in processes)}
            data["memory_protection"] = {**self.tasks.memory, "latched": self.tasks.memory_latched}
            data["autostart"] = autostart_status(self.project_root, self.port)
            data["settings"]["lan_enabled"] = self.settings.get("lan_enabled", False)
            data["port"] = self.port
            startup_args = [*runtime_command(), "--project", str(self.project_root), "--port", str(self.port)]
            data["startup_command"] = subprocess.list2cmdline(startup_args) if os.name == "nt" else shlex.join(startup_args)
            selected = next((t for t in tasks if t["processes"] and t["status"] != "COMPLETED"),
                            tasks[0] if tasks else None)
            if selected:
                data["job"] = selected["job"]
                data["results"] = selected["results"]
                data["health"] = selected["status"]
                data["message"] = "内存保护已关闭自动续跑" if self.tasks.memory_latched else selected["name"]
                data["recovery"].update(command_available=self.tasks.recoverable(selected),
                                        attempts=selected["attempts"], cwd=selected["cwd"],
                                        captured_at=iso_time(selected["started"]),
                                        next_restart_at=iso_time(selected["next_restart"]),
                                        last_restart_log=selected["log_path"],
                                        last_message="自动续跑开启" if selected["auto_restart"] else "自动续跑关闭")
            else:
                data["health"], data["message"] = "WAITING", "等待项目内的 MATLAB / Python 任务"
                data["job"] = snapshot_dict(MonitorSnapshot(time.time(), [], JobInfo(), "WAITING", ""))["job"]
                data["recovery"].update(command_available=False, attempts=0, cwd=None, captured_at=None,
                                        next_restart_at=None, last_restart_log=None, last_message="暂无任务")
            return data


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ResearchSentinel/1.0"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Browsers routinely abandon polling or asset requests during reloads.
            return

    @property
    def supervisor(self) -> ResearchSupervisor:
        return self.server.supervisor  # type: ignore[attr-defined]

    @property
    def web_root(self) -> Path:
        return self.server.web_root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 1024 * 1024)
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        candidate = (self.web_root / relative).resolve()
        try:
            candidate.relative_to(self.web_root.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _serve_artifact(self, raw_path: str) -> None:
        candidate = Path(raw_path).resolve()
        extension = candidate.suffix.lower()
        if (
            not is_within(candidate, self.supervisor.project_root)
            or not candidate.is_file()
            or extension not in RESULT_EXTENSIONS
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            size = candidate.stat().st_size
            handle = candidate.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(candidate.name)}")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extension == ".svg":
            self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        with handle:
            shutil.copyfileobj(handle, self.wfile)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/status":
            self._json(self.supervisor.status())
            return
        if path == "/api/artifact":
            values = parse_qs(parsed.query).get("path") or []
            if not values:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            self._serve_artifact(values[0])
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._json({"message": "拒绝跨站操作"}, 403)
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json({"message": "需要 JSON 请求"}, 415)
            return
        path = urlparse(self.path).path
        payload = self._read_json()
        if not isinstance(payload, dict):
            self._json({"message": "无效请求"}, 400)
            return
        if path == "/api/settings":
            try:
                changed = self.supervisor.update_settings(payload)
                self._json({"ok": True, "network_changed": changed})
                if changed and self.supervisor.rebind:
                    threading.Thread(target=self.supervisor.rebind, daemon=True).start()
            except (ValueError, OSError) as error:
                self._json({"message": str(error)}, 400)
            return
        if path in {"/api/task/settings", "/api/task/delete"}:
            try:
                with self.supervisor.lock:
                    if path.endswith("/delete"):
                        stop = payload.get("stop", False)
                        if not isinstance(stop, bool):
                            raise ValueError("stop 必须是布尔值")
                        self.supervisor.tasks.delete(str(payload.get("id")), stop)
                    else:
                        self.supervisor.tasks.configure(str(payload.get("id")), payload)
                self._json({"ok": True})
                self.supervisor.rescan()
            except (KeyError, ValueError, OSError, psutil.Error) as error:
                self._json({"message": str(error)}, 400)
            return
        if path == "/api/action/auto-restart":
            self._json({"message": "请在任务卡片上单独设置自动续跑"}, 409)
            return
        if path == "/api/action/restart":
            self._json({"message": "请使用任务级自动续跑"}, 409)
            return
        if path == "/api/action/rescan":
            self.supervisor.rescan()
            self._json({"ok": True})
            return
        self._json({"ok": False, "message": "接口不存在"}, 404)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], supervisor: ResearchSupervisor, web_root: Path):
        self.supervisor = supervisor
        self.web_root = web_root
        super().__init__(address, DashboardHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Web monitoring and recovery for MATLAB and Python research jobs")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--watch", action="append", type=Path, default=[])
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--stall-minutes", type=float, default=10.0)
    parser.add_argument("--lookback-hours", type=float, default=48.0)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="persistent data directory (default: data beside the program)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project.resolve()
    watch_roots = [path.resolve() for path in args.watch] if args.watch else default_watch_roots(project_root)
    selected_data_dir = args.data_dir.resolve() if args.data_dir else default_data_dir()
    selected_data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RESEARCH_SENTINEL_DATA_DIR"] = str(selected_data_dir)
    legacy_dir = project_root / ".matlab-monitor"
    if selected_data_dir.resolve() != legacy_dir.resolve():
        migrate_legacy_data(legacy_dir, selected_data_dir)
        migrate_legacy_progress(project_root, selected_data_dir)
    supervisor = ResearchSupervisor(project_root, watch_roots, max(0.5, args.interval), args.stall_minutes,
                                    args.lookback_hours, selected_data_dir)
    web_root = bundled_web_dir()
    supervisor.port = args.port
    if "lan_enabled" not in supervisor.settings:
        supervisor.settings["lan_enabled"] = bool(args.host and args.host not in {"127.0.0.1", "localhost"})
        atomic_json_write(supervisor.settings_path, supervisor.settings)
    started = False
    last_host = None
    try:
        while True:
            host = "0.0.0.0" if supervisor.settings.get("lan_enabled") else "127.0.0.1"
            try:
                server = DashboardServer((host, args.port), supervisor, web_root)
            except OSError as error:
                if last_host is None or last_host == host:
                    raise
                with supervisor.lock:
                    supervisor.settings["lan_enabled"] = last_host == "0.0.0.0"
                    atomic_json_write(supervisor.settings_path, supervisor.settings)
                    supervisor._record_event("network_error", f"监听切换失败，已恢复原地址：{error}")
                server = DashboardServer((last_host, args.port), supervisor, web_root)
                host = last_host
            if not started:
                supervisor.start()
                started = True
            last_host = host
            supervisor.rebind = server.shutdown
            print(f"Research Sentinel: http://{host}:{args.port}", flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                server.server_close()
    except KeyboardInterrupt:
        pass
    finally:
        if started:
            supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
