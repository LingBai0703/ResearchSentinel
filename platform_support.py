"""Platform-specific MATLAB process, launch, and diagnostic helpers."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


MATLAB_ARCH_DIRS = {"win64", "glnxa64", "maci64", "maca64"}
RECOVERY_FLAGS = {"-batch", "-r"}


def normalized_system(value: Optional[str] = None) -> str:
    name = (value or platform.system()).strip().lower()
    if name.startswith("win"):
        return "windows"
    if name in {"darwin", "mac", "macos"}:
        return "macos"
    if name == "linux":
        return "linux"
    return name or "unknown"


def normalized_machine(value: Optional[str] = None) -> str:
    machine = (value or platform.machine()).strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    return aliases.get(machine, machine or "unknown")


def platform_profile(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    release: Optional[str] = None,
) -> dict[str, Any]:
    os_name = normalized_system(system)
    architecture = normalized_machine(machine)
    os_release = release if release is not None else platform.release()
    current_official = False
    compatibility_mode = False
    target = "unsupported"
    label = f"{os_name} {architecture}"

    if os_name == "windows" and architecture in {"x86_64", "arm64"}:
        current_official = True
        target = "windows-arm64-prism" if architecture == "arm64" else "windows-x86_64"
        label = "Windows ARM (Prism)" if architecture == "arm64" else "Windows x86-64"
    elif os_name == "linux" and architecture == "x86_64":
        current_official = True
        target = "linux-x86_64"
        label = "Linux x86-64"
    elif os_name == "macos" and architecture == "arm64":
        current_official = True
        target = "macos-apple-silicon"
        label = "macOS Apple Silicon"
    elif os_name == "macos" and architecture == "x86_64":
        compatibility_mode = True
        target = "macos-intel"
        label = "macOS Intel"

    adapter_supported = current_official or compatibility_mode
    if current_official:
        support_label = "MATLAB R2026a 当前支持"
    elif compatibility_mode:
        support_label = "Intel Mac 兼容模式（最高 MATLAB R2025b）"
    else:
        support_label = "不在适配目标内"
    return {
        "system": os_name,
        "machine": architecture,
        "release": os_release,
        "target": target,
        "label": label,
        "adapter_supported": adapter_supported,
        "current_matlab_official": current_official,
        "compatibility_mode": compatibility_mode,
        "support_label": support_label,
    }


def is_matlab_process_name(name: str) -> bool:
    stem = Path(name or "").stem.lower()
    return stem == "matlab" or bool(re.fullmatch(r"matlab_r\d{4}[ab]", stem))


def is_matlab_executable(path: str | Path) -> bool:
    return is_matlab_process_name(Path(path).name)


def has_recovery_command(argv: Sequence[str]) -> bool:
    return any(str(value).lower() in RECOVERY_FLAGS for value in argv[1:])


def is_architecture_binary(path: str | Path) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    return bool(parts & MATLAB_ARCH_DIRS)


def canonical_matlab_launcher(executable: str | Path) -> Path:
    path = Path(executable)
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part.lower() not in MATLAB_ARCH_DIRS or index == 0:
            continue
        parent = Path(*parts[:index])
        names = ("matlab.exe", "matlab") if os.name == "nt" else ("matlab", "matlab.exe")
        for name in names:
            candidate = parent / name
            if candidate.exists():
                return candidate
    return path


def select_recovery_candidate(
    candidates: Sequence[dict[str, Any]],
    matlab_pids: Iterable[int],
) -> Optional[dict[str, Any]]:
    pids = set(matlab_pids)
    usable: list[tuple[int, dict[str, Any]]] = []
    for candidate in candidates:
        argv = [str(value) for value in candidate.get("argv") or []]
        executable = str(candidate.get("executable") or (argv[0] if argv else ""))
        if not argv or not is_matlab_executable(executable) or not has_recovery_command(argv):
            continue
        launcher = canonical_matlab_launcher(executable)
        normalized = dict(candidate)
        normalized["argv"] = [str(launcher), *argv[1:]]
        normalized["executable"] = str(launcher)
        score = 0
        if int(candidate.get("ppid") or 0) not in pids:
            score += 4
        if not is_architecture_binary(executable):
            score += 2
        if launcher != Path(executable):
            score += 1
        usable.append((score, normalized))
    return max(usable, key=lambda item: item[0])[1] if usable else None


def detached_popen_options(system: Optional[str] = None) -> dict[str, Any]:
    os_name = normalized_system(system)
    if os_name == "windows":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200),
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def _run_text(command: Sequence[str], timeout: float = 12) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout or completed.stderr).strip()
        return {
            "command": list(command),
            "returncode": completed.returncode,
            "output": output[-30000:],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": list(command), "error": str(error), "output": ""}


def _recent_mac_crash_reports(now: float) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    roots = (
        Path.home() / "Library" / "Logs" / "DiagnosticReports",
        Path("/Library/Logs/DiagnosticReports"),
        Path("/Library/Logs/CrashReporter"),
    )
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("MATLAB*"):
            try:
                stat = path.stat()
                if now - stat.st_mtime > 24 * 3600 or not path.is_file():
                    continue
                reports.append(
                    {
                        "path": str(path),
                        "modified": stat.st_mtime,
                        "tail": path.read_text(encoding="utf-8", errors="replace")[-20000:],
                    }
                )
            except OSError:
                continue
    return sorted(reports, key=lambda item: item["modified"], reverse=True)[:5]


def collect_platform_diagnostics(system: Optional[str] = None) -> dict[str, Any]:
    os_name = normalized_system(system)
    result: dict[str, Any] = {
        "platform": platform_profile(system=os_name),
        "commands": [],
        "crash_reports": [],
    }
    if os_name == "windows":
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell:
            script = (
                "$start=(Get-Date).AddMinutes(-10);"
                "Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$start} "
                "-ErrorAction SilentlyContinue | Where-Object {"
                "$_.ProviderName -match 'Application Error|Windows Error Reporting' -or "
                "$_.Message -match 'MATLAB'} | Select-Object -First 20 "
                "TimeCreated,Id,ProviderName,Message | ConvertTo-Json -Compress"
            )
            result["commands"].append(_run_text([powershell, "-NoProfile", "-Command", script]))
    elif os_name == "linux":
        journalctl = shutil.which("journalctl")
        if journalctl:
            result["commands"].append(
                _run_text(
                    [journalctl, "--since", "10 minutes ago", "--no-pager", "-n", "80", "--grep", "MATLAB|matlab"]
                )
            )
        coredumpctl = shutil.which("coredumpctl")
        if coredumpctl:
            result["commands"].append(
                _run_text([coredumpctl, "--no-pager", "--since", "10 minutes ago", "list", "MATLAB"])
            )
    elif os_name == "macos":
        log_tool = Path("/usr/bin/log")
        if log_tool.exists():
            result["commands"].append(
                _run_text(
                    [
                        str(log_tool),
                        "show",
                        "--last",
                        "10m",
                        "--style",
                        "compact",
                        "--predicate",
                        'process == "MATLAB" OR eventMessage CONTAINS[c] "MATLAB"',
                    ],
                    timeout=20,
                )
            )
        result["crash_reports"] = _recent_mac_crash_reports(time.time())
    return result
