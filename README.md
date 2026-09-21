# Research Sentinel

Research Sentinel 是用于 MATLAB 与 Python 科研计算的本地网页监控器。它发现已经启动且工作目录位于指定项目内的任务，显示进度、资源、结果图、日志与事件，并可按任务控制自动续跑。

## 下载单文件版本

GitHub Actions 会生成四种单文件产物：

- `ResearchSentinel-windows-x86_64`：Windows x86-64，也可在 Windows ARM 上通过 Prism 运行。
- `ResearchSentinel-linux-x86_64`：Linux x86-64。
- `ResearchSentinel-macos-x86_64`：Intel macOS。
- `ResearchSentinel-macos-arm64`：Apple Silicon macOS。

从 Actions 的构建记录下载对应产物，放入一个独立目录后运行：

```text
ResearchSentinel[.exe] --project "/path/to/research" --port 8765
```

程序会在可执行文件同目录创建 `data/`。设置、任务历史、事件、恢复日志和 Python 进度记录都保存在这里。移动程序时应连同 `data/` 一起移动，不要将该目录提交到公开仓库。

旧版本位于科研项目中的 `.matlab-monitor/` 和 `.research-progress/` 会在首次启动时迁移到新的 `data/` 布局。

## 从源码运行

需要 Python 3.10+：

```text
python -m pip install -r requirements.txt
python research_server.py --project "/path/to/research" --port 8765
```

Windows 可运行 `start_research_sentinel.ps1`，Linux/macOS 可运行 `start_research_sentinel.sh`。Shell 脚本使用这些可选变量：

```text
RESEARCH_SENTINEL_PROJECT=/path/to/research
RESEARCH_SENTINEL_HOST=0.0.0.0
RESEARCH_SENTINEL_PORT=8765
```

直接运行默认仅监听 `127.0.0.1`。在设置页打开网络访问后会切换到 `0.0.0.0`，但不会自动修改防火墙。服务没有账号认证，只应在可信局域网或受控 Tailscale 网络中使用。

## 本地编译

Windows：

```powershell
.\build.ps1 -Clean
```

Linux/macOS：

```bash
sh ./build.sh
```

打包入口为 `ResearchSentinel.spec`，结果位于 `dist/ResearchSentinel[.exe]`。`.github/workflows/build.yml` 会先运行 Python 测试，再在 Windows、Linux、Intel macOS 和 Apple Silicon macOS 上分别打包并执行 `--help` 冒烟测试。推送 `v*` 标签时会创建 GitHub Release。

## 自动续跑与历史

- 新任务默认开启自动续跑，每张任务卡可独立关闭。
- 只有明确未完成、明确失败或监控器重启的子进程非零退出时才会续跑；未知退出不会盲目重启。
- 重启会重放原命令。真正从断点继续依赖 MATLAB/Python 脚本自行保存和读取 checkpoint。
- 内存使用达到设置阈值时关闭所有自动续跑，不终止正在执行的计算，也不会自动重新开启。
- 已完成、异常退出和退出状态未知的任务默认保留 24 小时，也可在网页中手动删除。
- 删除任务默认不停止计算；勾选后才会校验 PID 和创建时间并终止对应进程。

## Python 进度

源码方式可在 Python 计算脚本中调用：

```python
from research_progress import report

report(project, completed, total)
report(project, total, total, state="completed")
# 自定义服务的 --data-dir 时可传 data_path="/path/to/data"。
```

最终结果和图像应在报告 `completed` 之前写入磁盘。也可以在任务详情中关联已有日志，支持 `completed=10/100` 格式。没有明确来源时页面显示“进度未知”。

## 自启动

设置页可以安装当前用户登录后的自启动配置：

- Windows：启动目录中的 `ResearchSentinel.vbs`。
- macOS Intel/Apple Silicon：`~/Library/LaunchAgents/io.research-sentinel.server.plist`。
- Linux：`~/.config/systemd/user/research-sentinel.service`。

Linux 无人登录运行需要管理员启用 linger。Windows/macOS 无人登录启动需要管理员配置系统级服务，具体步骤显示在设置页。
