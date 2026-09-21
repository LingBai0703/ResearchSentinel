"""Durable task identities, per-task recovery and memory protection."""
from __future__ import annotations

import json
import datetime as dt
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import psutil

from monitor_core import JobInfo, MonitorSnapshot, parse_progress_file, read_tail
from platform_support import canonical_matlab_launcher, detached_popen_options, is_matlab_process_name


def within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def identity(pid, started):
    return f"{pid}:{started:.3f}"


def python_command(argv):
    """Exclude REPLs, inline commands, worker helpers and this monitor."""
    if not argv or not re.fullmatch(r"python(?:w|\d+(?:\.\d+)*)?(?:\.exe)?", Path(argv[0]).name.lower()):
        return False
    if any("multiprocessing" in arg or "ipykernel" in arg for arg in argv):
        return False
    if "-c" in argv or "-" in argv:
        return False
    if "-m" in argv:
        index = argv.index("-m")
        module = argv[index + 1].lower() if index + 1 < len(argv) else ""
        tooling = {"pip", "pytest", "unittest", "compileall", "pyinstaller", "venv",
                   "ensurepip", "build", "twine", "http"}
        if module.split(".", 1)[0] in tooling:
            return False
    return any(arg.endswith(".py") for arg in argv[1:]) or "-m" in argv


class TaskStore:
    def __init__(self, root, state_dir, settings, save_json, discover_results, snapshot_dict):
        self.root, self.state_dir, self.settings = Path(root), Path(state_dir), settings
        self.save_json = save_json
        self.discover_results, self.snapshot_dict = discover_results, snapshot_dict
        self.path = self.state_dir / "tasks.json"
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.tasks = saved.get("tasks", {})
        self.ignored = saved.get("ignored", {})
        self.memory_latched = saved.get("memory_latched", False)
        self.memory = {}
        self.samples = {}
        self.children = {}
        self.last_save = 0

    def save(self):
        self.save_json(self.path, {"tasks": self.tasks, "ignored": self.ignored,
                                  "memory_latched": self.memory_latched})
        self.last_save = time.time()

    def import_completed(self, snapshot, command):
        """One-time migration of a verified completed legacy MATLAB run."""
        if self.path.exists() or self.tasks or not command or not snapshot.job.progress.complete:
            return
        directory = snapshot.job.directory
        if not directory or str(directory) != command.get("job_directory"):
            return
        ended = snapshot.job.latest_activity
        if not ended or time.time() - ended >= self.settings.get("retention_hours", 24) * 3600:
            return
        try:
            started = dt.datetime.fromisoformat(command["captured_at"]).timestamp()
        except (ValueError, KeyError):
            return
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "kind": "matlab", "name": "MATLAB（历史记录）",
                "argv": command.get("argv", []), "cwd": command.get("cwd", str(self.root)),
                "started": min(started, ended), "identities": [], "processes": [],
                "status": "COMPLETED", "ended": ended, "auto_restart": not self.memory_latched,
                "attempts": 0, "next_restart": None, "events": [],
                "results": self.discover_results(self.root, snapshot, started),
                "log_path": str(snapshot.job.progress.source or ""), "output_dir": str(directory),
                "last_seen": ended, "job": self.snapshot_dict(snapshot)["job"]}
        self.tasks[task_id] = task
        self.event(task, "已从旧版完成日志导入；开始时间采用原命令捕获时间")
        self.save()

    def event(self, task, message):
        task["events"].insert(0, {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "message": message})
        del task["events"][40:]

    def check_memory(self, memory=None):
        memory = memory or psutil.virtual_memory()
        threshold = self.settings.get("memory_limit_percent", 92)
        low = memory.percent >= threshold or memory.available < self.settings.get("memory_min_mb", 512) * 1024**2
        self.memory = {"percent": memory.percent, "available": memory.available, "low": low}
        if low:
            changed = not self.memory_latched
            self.memory_latched = True
            for task in self.tasks.values():
                if task["auto_restart"]:
                    changed = True
                    self.event(task, "内存保护：已关闭自动续跑，需要手动重新开启")
                task["auto_restart"] = False
                task["next_restart"] = None
            if changed:
                self.save()
        return low

    def sample(self):
        rows = {}
        own = psutil.Process()
        excluded = {own.pid}
        try:
            recovered = {child.pid for child in self.children.values()}
            for child in list(self.children.values()):
                try:
                    recovered.update(p.pid for p in psutil.Process(child.pid).children(recursive=True))
                except psutil.Error:
                    pass
            excluded.update(p.pid for p in own.children(recursive=True) if p.pid not in recovered)
        except psutil.Error:
            pass
        for p in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "cwd", "create_time"]):
            try:
                info = p.info
                argv = info["cmdline"] or []
                if info["pid"] in excluded:
                    continue
                kind = "matlab" if is_matlab_process_name(info["name"] or "") else "python"
                python_runtime = re.fullmatch(r"python(?:w|\d+(?:\.\d+)*)?(?:\.exe)?", (info["name"] or "").lower())
                if kind == "python" and not python_runtime:
                    continue
                cwd = info["cwd"] or ""
                if kind == "python" and any(Path(a).name in {"research_server.py", "monitor_core.py"} for a in argv):
                    continue
                if not cwd or not within(cwd, self.root):
                    continue
                key = identity(p.pid, info["create_time"])
                sampler = self.samples.setdefault(key, p)
                row = {"pid": p.pid, "ppid": info["ppid"], "started": info["create_time"],
                       "name": info["name"], "executable": info["exe"] or (argv[0] if argv else ""),
                       "argv": argv, "cwd": cwd, "kind": kind, "identity": key,
                       "eligible": kind == "matlab" or python_command(argv),
                       "cpu_percent": sampler.cpu_percent(), "memory_bytes": p.memory_info().rss,
                       "status": p.status()}
                rows[p.pid] = row
            except (psutil.Error, OSError):
                continue
        retained = {}
        for pid, row in rows.items():
            ancestor, visited = row, set()
            while not ancestor["eligible"] and ancestor["ppid"] in rows and ancestor["pid"] not in visited:
                visited.add(ancestor["pid"])
                ancestor = rows[ancestor["ppid"]]
            if ancestor["eligible"]:
                retained[pid] = row
        return retained

    def poll(self, fallback=None, rows=None, now=None):
        now = now or time.time()
        self.check_memory()
        rows = self.sample() if rows is None else rows
        present = {row["identity"] for row in rows.values()}
        self.samples = {key: value for key, value in self.samples.items() if key in present}
        # Keep deleted live processes suppressed, including across server restarts.
        self.ignored = {key: value for key, value in self.ignored.items()
                        if key in present or now - value < 86400}
        groups = {}
        for row in rows.values():
            parent = row
            visited = set()
            while parent["ppid"] in rows and parent["ppid"] not in visited:
                visited.add(parent["pid"])
                parent = rows[parent["ppid"]]
            groups.setdefault(parent["identity"], []).append(row)
        known = {key: task for task in self.tasks.values() for key in task["identities"]}
        matlab_groups = [g for g in groups.values() if g[0]["kind"] == "matlab"]
        seen = set()
        for root_key, group in groups.items():
            if any(r["identity"] in self.ignored for r in group):
                continue
            task = next((known[r["identity"]] for r in group if r["identity"] in known), None)
            if task is None:
                root = next((r for r in group if r["identity"] == root_key), group[0])
                argv = list(root["argv"])
                if root["kind"] == "matlab" and argv:
                    argv[0] = str(canonical_matlab_launcher(root["executable"]))
                elif argv:
                    argv[0] = root["executable"]
                task_id = uuid.uuid4().hex
                task = {"id": task_id, "kind": root["kind"], "name": Path(argv[1]).name if root["kind"] == "python" and len(argv) > 1 else "MATLAB",
                        "argv": argv, "cwd": root["cwd"], "started": root["started"],
                        "identities": [], "processes": [], "status": "RUNNING", "ended": None,
                        "auto_restart": not self.memory_latched, "attempts": 0, "next_restart": None,
                        "events": [], "results": [], "log_path": "", "output_dir": "",
                        "last_seen": now, "job": self.snapshot_dict(MonitorSnapshot(now, [], JobInfo(), "RUNNING", ""))["job"]}
                self.tasks[task_id] = task
                self.event(task, "已发现启动中的任务")
                if root["kind"] == "python":
                    try:
                        logs = [Path(f.path) for f in psutil.Process(root["pid"]).open_files()
                                if f.path.endswith((".log", ".out")) and within(f.path, self.root)]
                        if len(logs) == 1:
                            task["log_path"] = str(logs[0])
                    except psutil.Error:
                        pass
            seen.add(task["id"])
            task["identities"] = sorted(set(task["identities"]) | {r["identity"] for r in group},
                                        key=lambda key: float(key.split(":")[1]))
            task["processes"] = group
            task["last_processes"] = group
            task["last_seen"] = now
            task["next_restart"] = None
            if task["status"] != "COMPLETED":
                task["status"] = "RUNNING"
                task["ended"] = None
            # Legacy MATLAB logs are attributed only when exactly one MATLAB job exists.
            if (task["kind"] == "matlab" and not task.get("manual_paths") and len(matlab_groups) == 1
                    and fallback and fallback.job.directory
                    and fallback.job.latest_activity and fallback.job.latest_activity >= task["started"] - 300):
                task["job"] = self.snapshot_dict(fallback)["job"]
                task["output_dir"] = str(fallback.job.directory)
                task["log_path"] = str(fallback.job.progress.source or "")
            self.update_progress(task, now)
        for task in list(self.tasks.values()):
            if task["id"] not in seen:
                task["processes"] = []
                self.update_progress(task, now)
                child = self.children.get(task["id"])
                code = child.poll() if child else None
                if task["status"] in {"RUNNING", "RESTARTING"} and now - task["last_seen"] > 5:
                    complete = task["job"]["progress"].get("complete") or code == 0
                    progress = task["job"]["progress"]
                    known_incomplete = (progress.get("total") is not None and progress.get("completed") is not None) or task.get("failure_reported")
                    task["status"] = "COMPLETED" if complete else ("CRASHED" if known_incomplete or (code is not None and code != 0) else "EXITED")
                    task["ended"] = now
                    self.event(task, "进程已退出：" + task["status"])
            if task["status"] == "CRASHED" and task["auto_restart"] and not self.memory_latched:
                if task["attempts"] < self.settings.get("max_restart_attempts", 5) and self.recoverable(task):
                    task["next_restart"] = task["next_restart"] or now + min(300, self.settings.get("restart_delay_seconds", 10) * 2**task["attempts"])
                    if now >= task["next_restart"]:
                        self.restart(task, now)
            if task["ended"] and now - task["ended"] >= self.settings.get("retention_hours", 24) * 3600:
                self.delete(task["id"], False)
        if now - self.last_save >= 5:
            self.save()

    def update_progress(self, task, now):
        # Optional explicit progress protocol; never infer a Python exit as a crash.
        for key in task["identities"][-20:]:
            pid = key.split(":")[0]
            report = self.state_dir / "progress" / f"{pid}.json"
            try:
                value = json.loads(report.read_text(encoding="utf-8"))
                if identity(int(pid), float(value["started"])) != key:
                    continue
                completed, total = int(value["completed"]), int(value["total"])
                if completed < 0 or total < 1 or completed > total:
                    continue
                task["job"]["progress"].update(completed=completed, total=total,
                    fraction=completed / total, complete=value.get("state") == "completed",
                    latest_line=str(value.get("message", "")))
                if value.get("state") == "failed":
                    task["failure_reported"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if task["log_path"] and within(task["log_path"], self.root):
            path = Path(task["log_path"])
            if path.is_file():
                try:
                    progress = parse_progress_file(path)
                    if progress.completed is not None or progress.success:
                        task["job"]["progress"] = self.snapshot_dict(MonitorSnapshot(now, [], JobInfo(progress=progress), "", ""))["job"]["progress"]
                    task["job"]["log_tail"] = read_tail(path, 30)
                except OSError:
                    pass
        if task["output_dir"] and within(task["output_dir"], self.root):
            snap = MonitorSnapshot(now, [], JobInfo(directory=Path(task["output_dir"])), "", "")
            task["results"] = self.discover_results(self.root, snap, task["started"])
            task["job"]["directory"] = task["output_dir"]
        if task["job"]["progress"].get("complete") and task["status"] != "COMPLETED":
            task["status"] = "COMPLETED"
            task["ended"] = now
            task["next_restart"] = None
            self.event(task, "任务完成；记录保留至到期或手动删除")

    def recoverable(self, task):
        argv = task["argv"]
        return bool(argv and Path(argv[0]).is_file() and
                    (python_command(argv) if task["kind"] == "python" else any(a in ("-batch", "-r") for a in argv)))

    def restart(self, task, now):
        if self.check_memory() or self.memory_latched or task["processes"] or not task["auto_restart"]:
            return
        task["attempts"] += 1
        task["next_restart"] = None
        log_dir = self.state_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"restart-{task['id']}-{task['attempts']}.stdout.log"
        try:
            with log.open("ab", buffering=0) as output:
                child = subprocess.Popen(task["argv"], cwd=task["cwd"], stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=output, **detached_popen_options())
            self.children[task["id"]] = child
            task["identities"].append(identity(child.pid, psutil.Process(child.pid).create_time()))
            task["status"], task["last_seen"], task["ended"] = "RESTARTING", now, None
            task["log_path"] = str(log)
            self.event(task, "已重放原启动命令；断点恢复由计算脚本负责")
        except (OSError, psutil.Error) as error:
            task["status"] = "CRASHED"
            self.event(task, f"重启失败：{error}")
        self.save()

    def configure(self, task_id, payload):
        task = self.tasks[task_id]
        updates = {}
        for name in ("log_path", "output_dir"):
            if name in payload:
                value = str(payload[name]).strip()
                if value and (not within(value, self.root) or not Path(value).exists()):
                    raise ValueError("日志和结果目录必须位于当前项目内且已存在")
                if value and (Path(value).is_file() if name == "output_dir" else Path(value).is_dir()):
                    raise ValueError("路径类型不正确")
                updates[name] = value
        if "auto_restart" in payload:
            if not isinstance(payload["auto_restart"], bool):
                raise ValueError("自动续跑必须是布尔值")
            if payload["auto_restart"] and self.check_memory():
                raise ValueError("内存仍不足，不能开启自动续跑")
            if payload["auto_restart"]:
                self.memory_latched = False
            task["auto_restart"] = payload["auto_restart"]
            task["next_restart"] = None
        if updates:
            task.update(updates)
            task["manual_paths"] = True
        self.event(task, "任务设置已更新")
        self.save()

    def delete(self, task_id, stop=False):
        task = self.tasks[task_id]
        if stop:
            task["auto_restart"] = False
            task["next_restart"] = None
            self.save()
            # PID reuse must never terminate an unrelated process.
            targets = []
            for key in task["identities"]:
                try:
                    p = psutil.Process(int(key.split(":")[0]))
                    if identity(p.pid, p.create_time()) == key:
                        p.terminate()
                        targets.append(p)
                except psutil.NoSuchProcess:
                    pass
            if targets:
                _, alive = psutil.wait_procs(targets, timeout=3)
                if alive:
                    self.event(task, "部分进程未响应终止请求，保留任务记录，自动续跑已关闭")
                    self.save()
                    raise ValueError("部分进程尚未停止，任务记录已保留；请检查进程后重试")
        for key in task["identities"]:
            self.ignored[key] = time.time()
        del self.tasks[task_id]
        self.save()
