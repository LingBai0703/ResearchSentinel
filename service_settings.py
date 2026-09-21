"""Per-user autostart adapters. No elevation or firewall changes are attempted."""
import os
import plistlib
import subprocess
from pathlib import Path

from app_paths import runtime_command
from platform_support import normalized_system


def autostart_spec(project, port, system=None, home=None):
    system = normalized_system(system)
    home = Path(home or Path.home())
    argv = [*runtime_command(), "--project", str(project), "--port", str(port)]
    if system == "windows":
        folder = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        path = folder / "Microsoft/Windows/Start Menu/Programs/Startup/ResearchSentinel.vbs"
        command = subprocess.list2cmdline(argv).replace('"', '""')
        content = f'Set shell = CreateObject("WScript.Shell")\r\nshell.Run "{command}", 0, False\r\n'
        return path, content.encode("utf-16"), []
    if system == "macos":
        path = home / "Library/LaunchAgents/io.research-sentinel.server.plist"
        content = plistlib.dumps({"Label": "io.research-sentinel.server", "ProgramArguments": argv,
                                  "RunAtLoad": True, "WorkingDirectory": str(project)})
        return path, content, []
    if system == "linux":
        path = home / ".config/systemd/user/research-sentinel.service"
        # systemd unit values are not shell commands; escape its own special characters.
        def quote(value):
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        content = ("[Unit]\nDescription=Research Sentinel\nAfter=network.target\n\n[Service]\n"
                   f"ExecStart={' '.join(quote(v) for v in argv)}\n"
                   f"WorkingDirectory={quote(str(project))}\nRestart=on-failure\n\n"
                   "[Install]\nWantedBy=default.target\n")
        return path, content.encode(), ["systemctl", "--user"]
    raise ValueError("当前平台不支持自动安装自启动")


def autostart_status(project, port):
    try:
        path, content, _ = autostart_spec(project, port)
        return {"installed": path.exists() and path.read_bytes() == content,
                "path": str(path), "mode": "用户登录后启动"}
    except OSError as error:
        return {"installed": False, "error": str(error), "mode": "用户登录后启动"}


def set_autostart(project, port, enabled):
    path, content, control = autostart_spec(project, port)
    old = path.read_bytes() if path.exists() else None
    if old is not None and old != content:
        raise ValueError("自启动位置已有其他项目或版本的配置，请按教程检查后再操作：" + str(path))
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            path.unlink(missing_ok=True)
        if control:
            subprocess.run([*control, "daemon-reload"], check=True, capture_output=True, timeout=10)
            subprocess.run([*control, "enable" if enabled else "disable", path.name],
                           check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        if old is not None:
            path.write_bytes(old)
        else:
            path.unlink(missing_ok=True)
        raise ValueError("无法安装自启动，请按设置页的平台教程操作：" + str(error)) from error


def validate_settings(payload, current):
    result = dict(current)
    bounds = {"refresh_seconds": (1, 60), "retention_hours": (1, 168),
              "restart_delay_seconds": (5, 300), "max_restart_attempts": (0, 20),
              "memory_limit_percent": (50, 99), "memory_min_mb": (128, 32768)}
    for key, (low, high) in bounds.items():
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} 必须是 {low} 到 {high} 的整数")
            result[key] = value
    for key in ("lan_enabled", "autostart"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
            result[key] = payload[key]
    return result
