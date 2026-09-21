#!/usr/bin/env python3
"""Monitoring core for already-running MATLAB jobs.

This module contains process, progress, checkpoint, and diagnostic parsing
shared by the web supervisor. Running it directly starts the web service.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import datetime as dt
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from platform_support import collect_platform_diagnostics, is_matlab_process_name

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only on an unprepared PC
    psutil = None


ANSI_RESET = "\x1b[0m"
ANSI_CLEAR = "\x1b[2J\x1b[H"
ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"

PROGRESS_PATTERNS = (
    re.compile(r"\bcompleted\s*[=:]\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"\bprogress\s*[=:]\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"\bresumed\s+(\d+)\s*/\s*(\d+)", re.I),
)
TRIAL_PATTERN = re.compile(r"\btrial\s*[=:]\s*(\d+)\s*/\s*(\d+)", re.I)
DURATION_PATTERN = re.compile(r"\btime\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)\s*s\b", re.I)
SUCCESS_PATTERN = re.compile(
    r"(?:^|\s)(?:[A-Z][A-Z0-9_]*_OK|Completed:\s|completed successfully)", re.I
)
INTERESTING_SUFFIXES = (
    ".stdout.log",
    ".stderr.log",
    ".status.log",
    ".matlab.log",
    "_progress.csv",
    "_checkpoint.mat",
)
PRUNED_DIRS = {
    ".git",
    ".agents",
    ".claude",
    ".codex_deps",
    ".codex-diagnostics",
    "data",
    ".matlab-monitor",
    "latex",
    "overleaf_upload",
    "release",
    "tmp",
}


@dataclass
class ProcessInfo:
    pid: int
    name: str
    started: float
    cpu_seconds: float
    cpu_percent: float
    memory_bytes: int
    status: str
    executable: str = ""


@dataclass
class Artifact:
    path: Path
    modified: float
    size: int


@dataclass
class ProgressInfo:
    completed: Optional[int] = None
    total: Optional[int] = None
    trial: Optional[int] = None
    trials: Optional[int] = None
    eta_seconds: Optional[float] = None
    item_seconds: Optional[float] = None
    source: Optional[Path] = None
    latest_line: str = ""
    tail: list[str] = field(default_factory=list)
    success: bool = False

    @property
    def fraction(self) -> Optional[float]:
        if self.completed is None or not self.total:
            return None
        return min(1.0, max(0.0, self.completed / self.total))

    @property
    def complete(self) -> bool:
        return bool(
            self.success
            or (
                self.completed is not None
                and self.total is not None
                and self.total > 0
                and self.completed >= self.total
            )
        )


@dataclass
class JobInfo:
    directory: Optional[Path] = None
    artifacts: list[Artifact] = field(default_factory=list)
    progress: ProgressInfo = field(default_factory=ProgressInfo)
    latest_activity: Optional[float] = None
    checkpoint: Optional[Artifact] = None
    stderr: Optional[Artifact] = None
    stderr_tail: list[str] = field(default_factory=list)


@dataclass
class MonitorSnapshot:
    timestamp: float
    processes: list[ProcessInfo]
    job: JobInfo
    health: str
    message: str


def enable_windows_terminal() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def color(text: str, code: str, enabled: bool = True) -> str:
    return f"\x1b[{code}m{text}{ANSI_RESET}" if enabled else text


def truncate(text: str, width: int) -> str:
    text = text.replace("\t", " ").strip()
    if width <= 1:
        return text[: max(0, width)]
    return text if len(text) <= width else text[: width - 1] + "…"


def human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def age_text(timestamp: Optional[float], now: Optional[float] = None) -> str:
    if timestamp is None:
        return "--"
    return human_duration(max(0.0, (now or time.time()) - timestamp)) + " 前"


def read_tail(path: Path, max_lines: int = 80, max_bytes: int = 128 * 1024) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
        return [line.rstrip() for line in text.splitlines()[-max_lines:]]
    except (OSError, PermissionError):
        return []


def parse_progress_lines(lines: Sequence[str], source: Optional[Path] = None) -> ProgressInfo:
    result = ProgressInfo(source=source, tail=[line for line in lines[-8:] if line.strip()])
    durations: list[float] = []
    progress_matches: list[tuple[int, int, str]] = []
    trial_matches: list[tuple[int, int]] = []
    success_candidate = False

    for line in lines:
        if SUCCESS_PATTERN.search(line):
            success_candidate = True
        duration = DURATION_PATTERN.search(line)
        if duration:
            durations.append(float(duration.group(1)))
        trial = TRIAL_PATTERN.search(line)
        if trial:
            trial_matches.append((int(trial.group(1)), int(trial.group(2))))
        for pattern in PROGRESS_PATTERNS:
            match = pattern.search(line)
            if match:
                current, total = int(match.group(1)), int(match.group(2))
                if total > 0 and 0 <= current <= total:
                    progress_matches.append((current, total, line.strip()))
                break

    if progress_matches:
        current, total, latest = progress_matches[-1]
        result.completed, result.total, result.latest_line = current, total, latest
    elif result.tail:
        result.latest_line = result.tail[-1]
    if trial_matches:
        result.trial, result.trials = trial_matches[-1]
    # Runners may print UNIT_OK before the formal calculation begins. A
    # success marker must not override explicit incomplete progress.
    result.success = success_candidate and (
        result.completed is None
        or result.total is None
        or result.completed >= result.total
    )
    if durations:
        recent = durations[-20:]
        result.item_seconds = statistics.median(recent)
        if result.completed is not None and result.total is not None:
            result.eta_seconds = max(0, result.total - result.completed) * result.item_seconds
    return result


def parse_csv_progress(path: Path) -> ProgressInfo:
    result = ProgressInfo(source=path)
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows or "Completed" not in rows[0]:
            return result
        result.completed = sum(int(float(row.get("Completed") or 0)) for row in rows)
        name = path.name.lower()
        channels = re.search(r"(\d+)channels", name)
        points = re.search(r"(\d+)points", name)
        if channels and points:
            result.total = int(channels.group(1)) * int(points.group(1))
        elif channels:
            result.total = int(channels.group(1)) * len(rows)
        result.latest_line = f"CSV 汇总：{result.completed}/{result.total or '?'}"
        result.tail = [result.latest_line]
    except (OSError, ValueError, TypeError):
        pass
    return result


def parse_progress_file(path: Path) -> ProgressInfo:
    if path.name.lower().endswith("_progress.csv"):
        return parse_csv_progress(path)
    return parse_progress_lines(read_tail(path), path)


def default_watch_roots(project_root: Path) -> list[Path]:
    roots: list[Path] = []
    for project_name in ("2026_with_DLC", "2026_target_angle", "2026_fixed", "2026"):
        project = project_root / project_name
        for relative in (Path("image") / "versions", Path("output")):
            candidate = project / relative
            if candidate.is_dir():
                roots.append(candidate)
    return roots or [project_root]


def scan_artifacts(roots: Sequence[Path], cutoff: float) -> list[Artifact]:
    found: dict[Path, Artifact] = {}
    for root in roots:
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in PRUNED_DIRS]
            current_path = Path(current)
            for name in files:
                lower = name.lower()
                if not lower.endswith(INTERESTING_SUFFIXES):
                    continue
                path = current_path / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                found[path] = Artifact(path=path, modified=stat.st_mtime, size=stat.st_size)
    return sorted(found.values(), key=lambda item: item.modified, reverse=True)


def choose_active_job(artifacts: Sequence[Artifact], process_started: Optional[float]) -> JobInfo:
    if not artifacts:
        return JobInfo()

    grouped: dict[Path, list[Artifact]] = {}
    for artifact in artifacts:
        grouped.setdefault(artifact.path.parent, []).append(artifact)

    def directory_score(item: tuple[Path, list[Artifact]]) -> tuple[float, int]:
        _, members = item
        recent = max(member.modified for member in members)
        after_start = sum(
            1 for member in members if process_started is None or member.modified >= process_started - 300
        )
        return recent, after_start

    directory, members = max(grouped.items(), key=directory_score)
    members = sorted(members, key=lambda item: item.modified, reverse=True)
    job = JobInfo(directory=directory, artifacts=members, latest_activity=members[0].modified)
    job.checkpoint = next(
        (item for item in members if item.path.name.lower().endswith("_checkpoint.mat")), None
    )

    eligible = [
        item
        for item in members
        if process_started is None or item.modified >= process_started - 300
    ] or members

    progress_candidates: list[tuple[Artifact, ProgressInfo]] = []
    for artifact in eligible[:30]:
        lower = artifact.path.name.lower()
        if lower.endswith((".stdout.log", ".matlab.log", "_progress.csv")):
            parsed = parse_progress_file(artifact.path)
            if parsed.completed is not None or parsed.latest_line:
                progress_candidates.append((artifact, parsed))
    if progress_candidates:
        # File recency identifies a resumed run correctly even when an older log
        # in the same directory already says 280/280.
        _, job.progress = max(progress_candidates, key=lambda item: item[0].modified)

    stderr_candidates = [
        item for item in eligible if item.path.name.lower().endswith(".stderr.log")
    ]
    if stderr_candidates:
        job.stderr = max(stderr_candidates, key=lambda item: item.modified)
        job.stderr_tail = [line for line in read_tail(job.stderr.path, 12) if line.strip()]
    return job


class ProcessSampler:
    def __init__(self) -> None:
        self.previous: dict[tuple[int, float], tuple[float, float]] = {}

    def sample(self, now: float) -> list[ProcessInfo]:
        if psutil is None:
            raise RuntimeError("缺少 psutil。请先执行：python -m pip install psutil")
        processes: list[ProcessInfo] = []
        current_keys: set[tuple[int, float]] = set()
        for process in psutil.process_iter(
            ["pid", "name", "create_time", "cpu_times", "memory_info", "status", "exe"]
        ):
            try:
                info = process.info
                name = str(info.get("name") or "")
                if not is_matlab_process_name(name):
                    continue
                started = float(info.get("create_time") or now)
                cpu_times = info.get("cpu_times")
                cpu_seconds = float(cpu_times.user + cpu_times.system) if cpu_times else 0.0
                key = (int(info["pid"]), started)
                previous = self.previous.get(key)
                cpu_percent = 0.0
                if previous and now > previous[0]:
                    cpu_percent = max(0.0, (cpu_seconds - previous[1]) / (now - previous[0]) * 100.0)
                self.previous[key] = (now, cpu_seconds)
                current_keys.add(key)
                memory_info = info.get("memory_info")
                processes.append(
                    ProcessInfo(
                        pid=int(info["pid"]),
                        name=name,
                        started=started,
                        cpu_seconds=cpu_seconds,
                        cpu_percent=cpu_percent,
                        memory_bytes=int(memory_info.rss) if memory_info else 0,
                        status=str(info.get("status") or "unknown"),
                        executable=str(info.get("exe") or ""),
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        self.previous = {key: value for key, value in self.previous.items() if key in current_keys}
        return sorted(processes, key=lambda item: (item.memory_bytes, item.cpu_seconds), reverse=True)


class MonitorState:
    def __init__(self, stall_seconds: float) -> None:
        self.stall_seconds = stall_seconds
        self.had_process = False
        self.low_cpu_since: Optional[float] = None
        self.last_snapshot: Optional[MonitorSnapshot] = None
        self.exit_reported = False

    def classify(self, now: float, processes: Sequence[ProcessInfo], job: JobInfo) -> tuple[str, str]:
        if not processes:
            self.low_cpu_since = None
            if self.had_process:
                if job.progress.complete:
                    return "COMPLETED", "MATLAB 已退出，任务有完成标记"
                return "CRASHED", "MATLAB 在任务完成前消失，已保存诊断快照"
            return "WAITING", "等待已经启动的 MATLAB 进程"

        self.had_process = True
        self.exit_reported = False
        total_cpu = sum(item.cpu_percent for item in processes)
        activity_age = now - job.latest_activity if job.latest_activity else math.inf
        active = total_cpu >= 1.0 or activity_age <= 30.0
        if active:
            self.low_cpu_since = None
            return "RUNNING", "计算活动正常"
        if self.low_cpu_since is None:
            self.low_cpu_since = now
        quiet_for = now - self.low_cpu_since
        if quiet_for >= self.stall_seconds and activity_age >= self.stall_seconds:
            return "STALLED", "CPU 和进度文件长时间没有变化，疑似卡住"
        return "IDLE", "MATLAB 存活，当前采样期活动较低"


def path_or_none(path: Optional[Path]) -> Optional[str]:
    return str(path) if path else None


def save_crash_report(state_dir: Path, snapshot: MonitorSnapshot) -> Path:
    crash_dir = state_dir / "crashes"
    crash_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(snapshot.timestamp).strftime("%Y%m%d_%H%M%S")
    path = crash_dir / f"matlab_exit_{stamp}.json"
    payload = {
        "captured_at": dt.datetime.fromtimestamp(snapshot.timestamp).isoformat(),
        "health": snapshot.health,
        "message": snapshot.message,
        "processes_before_exit": [asdict(item) for item in snapshot.processes],
        "job_directory": path_or_none(snapshot.job.directory),
        "progress": {
            **asdict(snapshot.job.progress),
            "source": path_or_none(snapshot.job.progress.source),
        },
        "latest_activity": snapshot.job.latest_activity,
        "checkpoint": path_or_none(snapshot.job.checkpoint.path) if snapshot.job.checkpoint else None,
        "stderr": path_or_none(snapshot.job.stderr.path) if snapshot.job.stderr else None,
        "stderr_tail": snapshot.job.stderr_tail,
        "artifacts": [
            {"path": str(item.path), "modified": item.modified, "size": item.size}
            for item in snapshot.job.artifacts[:30]
        ],
        "platform_diagnostics": collect_platform_diagnostics(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def append_event(state_dir: Path, snapshot: MonitorSnapshot, report: Optional[Path] = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": dt.datetime.fromtimestamp(snapshot.timestamp).isoformat(),
        "health": snapshot.health,
        "message": snapshot.message,
        "pids": [item.pid for item in snapshot.processes],
        "progress": [snapshot.job.progress.completed, snapshot.job.progress.total],
        "job_directory": path_or_none(snapshot.job.directory),
        "report": path_or_none(report),
    }
    with (state_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def progress_bar(fraction: Optional[float], width: int) -> str:
    width = max(10, width)
    if fraction is None:
        return "[" + "·" * width + "]"
    filled = min(width, max(0, round(width * fraction)))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def render(snapshot: MonitorSnapshot, project_root: Path, color_enabled: bool = True) -> str:
    width, height = shutil.get_terminal_size((110, 32))
    width = max(72, width)
    now = snapshot.timestamp
    palette = {
        "RUNNING": ("运行中", "32;1"),
        "IDLE": ("低活动", "33;1"),
        "STALLED": ("疑似卡住", "31;1"),
        "WAITING": ("等待进程", "36;1"),
        "COMPLETED": ("正常完成", "32;1"),
        "CRASHED": ("异常退出", "31;1"),
    }
    label, label_color = palette.get(snapshot.health, (snapshot.health, "37;1"))
    title = " MATLAB 运行监控 "
    lines = [
        color(title.center(width, "═"), "36;1", color_enabled),
        f"状态  {color(label, label_color, color_enabled)}    {snapshot.message}",
        f"时间  {dt.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}    项目  {truncate(str(project_root), max(20, width - 48))}",
        "─" * width,
        color("进程", "36;1", color_enabled),
    ]

    if snapshot.processes:
        lines.append(f"{'PID':>7}  {'角色':<8} {'CPU':>8} {'内存':>11} {'运行时长':>14}  状态")
        for index, process in enumerate(snapshot.processes[:6]):
            role = "主计算" if index == 0 else "辅助"
            uptime = human_duration(now - process.started)
            lines.append(
                f"{process.pid:>7}  {role:<8} {process.cpu_percent:>7.1f}% "
                f"{human_bytes(process.memory_bytes):>11} {uptime:>14}  {process.status}"
            )
    else:
        lines.append("未检测到 MATLAB。监控器保持等待，不会主动启动它。")

    lines.extend(["─" * width, color("任务进度", "36;1", color_enabled)])
    progress = snapshot.job.progress
    fraction = progress.fraction
    percent = f"{fraction * 100:5.1f}%" if fraction is not None else "  -- %"
    count = (
        f"{progress.completed}/{progress.total}"
        if progress.completed is not None and progress.total is not None
        else "--/--"
    )
    bar_width = max(12, min(54, width - 38))
    lines.append(f"{progress_bar(fraction, bar_width)}  {percent}  {count}")
    trial = (
        f"信道/轮次 {progress.trial}/{progress.trials}"
        if progress.trial is not None and progress.trials is not None
        else "信道/轮次 --"
    )
    item = f"单点约 {progress.item_seconds:.1f} 秒" if progress.item_seconds else "单点耗时 --"
    eta = f"预计剩余 {human_duration(progress.eta_seconds)}" if progress.eta_seconds is not None else "预计剩余 --"
    lines.append(f"{trial}    {item}    {eta}")
    if progress.latest_line:
        lines.append("当前  " + truncate(progress.latest_line, width - 6))

    lines.extend(["─" * width, color("监控来源", "36;1", color_enabled)])
    if snapshot.job.directory:
        lines.append("任务  " + truncate(str(snapshot.job.directory), width - 6))
    else:
        lines.append("任务  尚未发现近期任务文件")
    if progress.source:
        lines.append(f"日志  {truncate(progress.source.name, width - 15):<{max(1, width - 15)}} 更新于 {age_text(snapshot.job.latest_activity, now)}")
    if snapshot.job.checkpoint:
        lines.append(
            f"检查点  {truncate(snapshot.job.checkpoint.path.name, width - 28)}  "
            f"{human_bytes(snapshot.job.checkpoint.size)}  {age_text(snapshot.job.checkpoint.modified, now)}"
        )
    if snapshot.job.stderr_tail:
        lines.append(color("错误输出  检测到非空内容", "31;1", color_enabled))
    elif snapshot.job.stderr:
        lines.append("错误输出  空")

    lines.extend(["─" * width, color("最近输出", "36;1", color_enabled)])
    available = max(3, height - len(lines) - 2)
    tail = progress.tail[-available:]
    if tail:
        lines.extend(truncate(line, width) for line in tail)
    else:
        lines.append("暂无可解析的进度输出")
    return "\n".join(lines[: max(height - 1, 10)])


def read_key() -> Optional[str]:
    if os.name == "nt":
        try:
            import msvcrt

            if msvcrt.kbhit():
                return msvcrt.getwch().lower()
        except OSError:
            return None
        return None
    try:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1).lower() if ready else None
    except (OSError, ValueError):
        return None


def make_snapshot(
    sampler: ProcessSampler,
    state: MonitorState,
    roots: Sequence[Path],
    lookback_hours: float,
) -> MonitorSnapshot:
    now = time.time()
    processes = sampler.sample(now)
    process_started = min((item.started for item in processes), default=None)
    cutoff = min(
        now - lookback_hours * 3600,
        (process_started - 24 * 3600) if process_started is not None else now,
    )
    artifacts = scan_artifacts(roots, cutoff)
    job = choose_active_job(artifacts, process_started)
    health, message = state.classify(now, processes, job)
    return MonitorSnapshot(now, processes, job, health, message)


def run_monitor(args: argparse.Namespace) -> int:
    project_root = args.project.resolve()
    roots = [path.resolve() for path in args.watch] if args.watch else default_watch_roots(project_root)
    from app_paths import data_dir

    state_dir = data_dir()
    sampler = ProcessSampler()
    state = MonitorState(args.stall_minutes * 60)
    last_health: Optional[str] = None
    force_scan = True
    cached_snapshot: Optional[MonitorSnapshot] = None

    enable_windows_terminal()
    if sys.stdout.isatty():
        sys.stdout.write(ANSI_HIDE_CURSOR)
    try:
        while True:
            if force_scan or cached_snapshot is None:
                snapshot = make_snapshot(sampler, state, roots, args.lookback_hours)
                cached_snapshot = snapshot
                force_scan = False
            else:
                snapshot = make_snapshot(sampler, state, roots, args.lookback_hours)
                cached_snapshot = snapshot

            if snapshot.health != last_health:
                append_event(state_dir, snapshot)
                last_health = snapshot.health

            if snapshot.health == "CRASHED" and not state.exit_reported:
                previous = state.last_snapshot or snapshot
                report_snapshot = MonitorSnapshot(
                    timestamp=snapshot.timestamp,
                    processes=previous.processes,
                    job=snapshot.job,
                    health=snapshot.health,
                    message=snapshot.message,
                )
                report = save_crash_report(state_dir, report_snapshot)
                append_event(state_dir, snapshot, report)
                state.exit_reported = True
                if sys.stdout.isatty():
                    sys.stdout.write("\a")

            state.last_snapshot = snapshot
            output = render(snapshot, project_root, color_enabled=not args.no_color)
            if args.once:
                print(output)
                return 2 if snapshot.health in {"CRASHED", "STALLED"} else 0
            sys.stdout.write(ANSI_CLEAR + output)
            sys.stdout.flush()

            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline:
                key = read_key()
                if key == "q":
                    return 0
                if key == "r":
                    force_scan = True
                    break
                time.sleep(0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        if sys.stdout.isatty():
            sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_RESET + "\n")
            sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读监控已启动的 MATLAB 进程和计算进度")
    parser.add_argument(
        "--project",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="项目根目录",
    )
    parser.add_argument(
        "--watch",
        action="append",
        type=Path,
        default=[],
        help="额外指定监控目录，可重复使用",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="刷新间隔（秒）")
    parser.add_argument("--stall-minutes", type=float, default=10.0, help="疑似卡住阈值（分钟）")
    parser.add_argument("--lookback-hours", type=float, default=48.0, help="任务文件回看时长")
    parser.add_argument("--once", action="store_true", help="采样一次后退出")
    parser.add_argument("--no-color", action="store_true", help="禁用 ANSI 颜色")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    from research_server import main as server_main

    return server_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
