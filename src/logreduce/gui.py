"""Local web GUI for logReduce.

The GUI is a thin control panel around the existing CLI.  It uses only the
Python standard library, binds to loopback by default, and runs the CLI in a
child process so GUI and CLI behaviour (and performance) stay identical.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import load_config
from .errors import ConfigError
from .rules import Ruleset

MAX_LOG_LINES = 20000
MAX_BODY_BYTES = 1 << 20


class LogBuffer:
    """Bounded line buffer with stable absolute indexes."""

    def __init__(self, max_lines: int = MAX_LOG_LINES) -> None:
        self._max_lines = max_lines
        self._lines: deque[tuple[int, str]] = deque()
        self._next_index = 0
        self._base_index = 0

    def append(self, line: str) -> None:
        if len(self._lines) >= self._max_lines:
            self._lines.popleft()
            self._base_index += 1
        self._lines.append((self._next_index, line))
        self._next_index += 1

    def since(self, index: int) -> tuple[list[str], int]:
        if index < self._base_index:
            index = self._base_index
        lines = [line for line_index, line in self._lines if line_index >= index]
        return lines, self._next_index

    def clear(self) -> None:
        self._lines.clear()
        self._next_index = 0
        self._base_index = 0


class JobManager:
    """Run one CLI job at a time and expose incremental output/status."""

    def __init__(self, max_lines: int = MAX_LOG_LINES) -> None:
        self._lock = threading.Lock()
        self._buffer = LogBuffer(max_lines=max_lines)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._returncode: int | None = None
        self._started: float | None = None
        self._finished: float | None = None
        self._command: list[str] = []
        self._error: str | None = None
        self._job_id = 0
        self._generation = 0

    def start(self, command: list[str]) -> int:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("已有任务正在运行")
            self._generation += 1
            generation = self._generation
            self._buffer.clear()
            self._returncode = None
            self._started = time.monotonic()
            self._finished = None
            self._command = list(command)
            self._error = None
            self._job_id += 1
            job_id = self._job_id
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    errors="replace",
                    start_new_session=(os.name != "nt"),
                    env=_child_env(),
                )
            except OSError as exc:
                self._process = None
                self._error = str(exc)
                self._returncode = -1
                self._finished = time.monotonic()
                self._buffer.append(f"无法启动进程: {exc}")
                raise

            self._process = process
            self._reader = threading.Thread(
                target=self._read_output,
                args=(process, generation),
                daemon=True,
            )
            self._reader.start()
            return job_id

    def _read_output(self, process: subprocess.Popen[str], generation: int) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self._append(generation, line.rstrip("\r\n"))
            returncode = process.poll()
            if returncode is None:
                returncode = process.wait()
            self._finish(generation, returncode)
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                if generation == self._generation:
                    self._error = str(exc)
                    self._returncode = -1
                    self._finished = time.monotonic()
                    self._buffer.append(f"[读取输出失败: {exc}]")
        finally:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except OSError:
                pass

    def _append(self, generation: int, line: str) -> None:
        with self._lock:
            if generation == self._generation:
                self._buffer.append(line)

    def _finish(self, generation: int, returncode: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._returncode = returncode
            self._finished = time.monotonic()
            if returncode == 0:
                self._buffer.append("[任务完成]")
            else:
                self._buffer.append(f"[任务结束，退出码 {returncode}]")

    def snapshot(self, since: int) -> dict[str, Any]:
        with self._lock:
            lines, next_index = self._buffer.since(since)
            running = self._process is not None and self._returncode is None
            started = self._started
            finished = self._finished
            elapsed = 0.0
            if started is not None:
                elapsed = (finished if finished is not None else time.monotonic()) - started
            return {
                "job_id": self._job_id,
                "running": running,
                "returncode": self._returncode,
                "error": self._error,
                "command": self._command,
                "elapsed": round(elapsed, 3),
                "lines": lines,
                "next": next_index,
            }

    def stop(self) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                try:
                    process.terminate()
                except OSError:
                    pass
        else:
            try:
                process.terminate()
            except OSError:
                pass
        threading.Thread(
            target=self._kill_if_needed,
            args=(process,),
            daemon=True,
        ).start()
        return True

    @staticmethod
    def _kill_if_needed(process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.1)
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass

    def wait_stopped(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
            if process is None or process.poll() is not None:
                return
            time.sleep(0.05)


def _child_env() -> dict[str, str]:
    """Make ``python -m logreduce`` work from a source checkout or install."""

    env = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        package_parent + (os.pathsep + existing if existing else "")
    )
    return env


def _text(payload: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"缺少字段: {key}")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    value = value.strip()
    if not value:
        if required:
            raise ValueError(f"{key} 不能为空")
        return None
    return value


def _flag(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    raise ValueError(f"{key} 必须是布尔值")


def build_command(payload: dict[str, Any]) -> list[str]:
    """Build the CLI argument vector from a GUI request.

    No shell is involved.  Paths and options are passed as separate argv
    entries, so user input cannot become a command separator.
    """

    action = payload.get("action")
    if action not in ("run", "dry-run", "validate"):
        raise ValueError("action 必须是 run、dry-run 或 validate")

    config = _text(payload, "config", required=True)
    assert config is not None

    if action == "validate":
        return [
            sys.executable,
            "-m",
            "logreduce",
            "--validate",
            "-c",
            config,
        ]

    log_dir = _text(payload, "log_dir", required=True)
    assert log_dir is not None
    output = _text(payload, "output")

    mode_value = payload.get("mode")
    if mode_value in (None, "", "default"):
        mode = None
    elif mode_value in ("inplace", "direct"):
        mode = mode_value
    else:
        raise ValueError("mode 必须是 default、inplace 或 direct")

    jobs_value = payload.get("jobs")
    jobs: int | None
    if jobs_value in (None, ""):
        jobs = None
    else:
        if isinstance(jobs_value, bool):
            raise ValueError("jobs 必须是 0-256 的整数")
        if isinstance(jobs_value, float):
            if not jobs_value.is_integer():
                raise ValueError("jobs 必须是 0-256 的整数")
            jobs_value = int(jobs_value)
        try:
            jobs = int(jobs_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("jobs 必须是 0-256 的整数") from exc
        if jobs < 0 or jobs > 256:
            raise ValueError("jobs 必须在 0-256 之间")

    command = [
        sys.executable,
        "-m",
        "logreduce",
        "-d",
        log_dir,
        "-c",
        config,
    ]
    if output:
        command.extend(["-o", output])
    if mode:
        command.extend(["--mode", mode])
    if jobs is not None:
        command.extend(["--jobs", str(jobs)])
    if action == "dry-run" or _flag(payload, "dry_run"):
        command.append("--dry-run")
    if _flag(payload, "keep_sources"):
        command.append("--keep-sources")
    if _flag(payload, "force"):
        command.append("--force")
    if _flag(payload, "no_fsync"):
        command.append("--no-fsync")
    if _flag(payload, "allow_changing"):
        command.append("--allow-changing")
    if _flag(payload, "verbose"):
        command.append("--verbose")
    command.append("--stats")
    return command


def config_summary(path: str) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    config = load_config(config_path)
    Ruleset(config.rules)
    return {
        "ok": True,
        "output": config.output.path or "",
        "mode": config.engine.mode,
        "jobs": config.engine.jobs,
        "rules": len(config.rules),
        "include": list(config.input.include),
        "exclude": list(config.input.exclude),
        "delete_sources": config.output.delete_sources,
    }


class GuiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], job_manager: JobManager) -> None:
        self.job_manager = job_manager
        super().__init__(server_address, GuiRequestHandler)


class GuiRequestHandler(BaseHTTPRequestHandler):
    server_version = "logReduceGUI/0.1"
    sys_version = ""

    @property
    def gui_server(self) -> GuiHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        # Keep the console clean; job output is shown in the browser.
        return

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError("Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体大小无效")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._json(200, {"ok": True, "version": __version__})
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "since 必须是整数"})
                return
            self._json(200, self.gui_server.job_manager.snapshot(since))
            return
        if parsed.path == "/api/config":
            query = parse_qs(parsed.query)
            path = query.get("path", [""])[0].strip()
            if not path:
                self._json(400, {"ok": False, "error": "缺少 path 参数"})
                return
            try:
                self._json(200, config_summary(path))
            except (ConfigError, OSError, ValueError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/job":
            try:
                payload = self._read_json()
                command = build_command(payload)
                job_id = self.gui_server.job_manager.start(command)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except RuntimeError as exc:
                self._json(409, {"ok": False, "error": str(exc)})
            except OSError as exc:
                self._json(500, {"ok": False, "error": f"启动失败: {exc}"})
            else:
                self._json(200, {"ok": True, "job_id": job_id, "command": command})
            return
        if parsed.path == "/api/stop":
            stopped = self.gui_server.job_manager.stop()
            self._json(200, {"ok": True, "stopped": stopped})
            return
        self._json(404, {"ok": False, "error": "not found"})


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    job_manager: JobManager | None = None,
) -> GuiHTTPServer:
    return GuiHTTPServer((host, port), job_manager or JobManager())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logreduce-gui",
        description="logReduce 的本地 Web GUI（仅监听本机回环地址更安全）。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动选择")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.port < 0 or args.port > 65535:
        parser.error("--port 必须在 0-65535 之间")

    manager = JobManager()
    try:
        server = create_server(args.host, args.port, manager)
    except OSError as exc:
        print(f"无法启动 GUI 服务: {exc}", file=sys.stderr)
        return 1

    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{server.server_port}/"
    print(f"logReduce GUI: {url}", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("警告: 正在监听非回环地址；不要将本服务暴露到不可信网络。", file=sys.stderr)

    if not args.no_browser:
        def open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Timer(0.3, open_browser).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        manager.stop()
        manager.wait_stopped(5.0)
        server.server_close()
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>logReduce GUI</title>
<style>
:root {
  --bg: #f4f7fb;
  --card: #ffffff;
  --ink: #172033;
  --muted: #667085;
  --line: #d9e2ec;
  --accent: #1677ff;
  --accent-dark: #0b5ed7;
  --green: #0f8a5f;
  --amber: #9a6700;
  --red: #b42318;
  --log-bg: #0e1726;
  --log-ink: #d8f3e6;
  --shadow: 0 10px 30px rgba(23, 32, 51, 0.08);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(1200px 500px at 10% -10%, #dce9ff 0%, transparent 55%),
    radial-gradient(900px 400px at 100% 0%, #e6fff4 0%, transparent 50%),
    var(--bg);
  color: var(--ink);
  font: 14px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
}
header {
  max-width: 1120px;
  margin: 0 auto;
  padding: 30px 24px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.brand { display: flex; align-items: center; gap: 14px; }
.logo {
  width: 46px; height: 46px; border-radius: 13px;
  display: grid; place-items: center;
  background: linear-gradient(135deg, #1677ff, #0f8a5f);
  color: #fff; font-weight: 800; letter-spacing: -1px;
  box-shadow: 0 8px 20px rgba(22, 119, 255, 0.28);
}
h1 { margin: 0; font-size: 24px; letter-spacing: -0.4px; }
.brand p { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.pill {
  border-radius: 999px; padding: 7px 13px; font-weight: 700; font-size: 12px;
  border: 1px solid transparent; white-space: nowrap;
}
.pill.idle { background: #eef2f6; color: #475467; }
.pill.running { background: #e8f1ff; color: var(--accent-dark); border-color: #c7ddff; }
.pill.ok { background: #e7f8f0; color: var(--green); border-color: #b7e9d3; }
.pill.error { background: #fdeceb; color: var(--red); border-color: #f5c6c2; }
main { max-width: 1120px; margin: 0 auto; padding: 0 24px 40px; }
.warning {
  margin: 8px 0 16px;
  padding: 12px 15px;
  border: 1px solid #f2d58b;
  background: #fffaeb;
  color: #7a4f01;
  border-radius: 12px;
  font-size: 13px;
}
.card {
  background: var(--card);
  border: 1px solid rgba(217, 226, 236, 0.9);
  border-radius: 16px;
  box-shadow: var(--shadow);
  padding: 20px;
  margin-top: 16px;
}
.card h2 { margin: 0 0 14px; font-size: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field.wide { grid-column: span 2; }
.field label { font-weight: 650; font-size: 13px; }
.field small { color: var(--muted); font-size: 12px; }
.req { color: var(--red); }
input[type="text"], input[type="number"], select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  background: #fbfdff;
  color: var(--ink);
  font: inherit;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
input[type="text"]:focus, input[type="number"]:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.13);
}
.input-row { display: flex; gap: 8px; }
.input-row input { flex: 1; }
button {
  border: 1px solid transparent;
  border-radius: 10px;
  padding: 10px 15px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
  transition: transform .05s, background .15s, border-color .15s;
}
button:active { transform: translateY(1px); }
button:disabled { opacity: .45; cursor: not-allowed; transform: none; }
button.primary { background: var(--accent); color: #fff; }
button.primary:hover:not(:disabled) { background: var(--accent-dark); }
button.ghost { background: #f7faff; border-color: #cfe0f7; color: #184a86; }
button.ghost:hover:not(:disabled) { background: #edf5ff; }
button.danger { background: #fff1f0; border-color: #f3c2bd; color: var(--red); }
button.danger:hover:not(:disabled) { background: #ffe4e1; }
button.mini { padding: 8px 11px; background: #f2f6fb; border-color: var(--line); color: #344054; font-weight: 600; }
button.mini:hover:not(:disabled) { background: #e9f1fb; }
.options { display: flex; flex-wrap: wrap; gap: 10px 20px; margin: 16px 0 4px; }
.options label { display: flex; align-items: center; gap: 7px; color: #344054; font-size: 13px; }
.options input { width: 16px; height: 16px; accent-color: var(--accent); }
.actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 16px; }
.log-head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.log-head h2 { margin: 0; flex: 1; }
.log-head .small { color: var(--muted); font-size: 12px; display: flex; gap: 6px; align-items: center; }
.log {
  margin: 0;
  min-height: 220px;
  max-height: 460px;
  overflow: auto;
  border-radius: 12px;
  background: var(--log-bg);
  color: var(--log-ink);
  padding: 14px 16px;
  font: 12.5px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 4;
}
.statusbar {
  display: flex; justify-content: space-between; gap: 12px;
  color: var(--muted); font-size: 12px; margin-top: 10px;
}
@media (max-width: 760px) {
  .grid { grid-template-columns: 1fr; }
  .field.wide { grid-column: span 1; }
  header { padding-top: 20px; }
  h1 { font-size: 21px; }
}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">LR</div>
    <div>
      <h1>logReduce</h1>
      <p>日志筛选 / 替换 / 合并 · 本地控制面板</p>
    </div>
  </div>
  <div id="status-pill" class="pill idle">空闲</div>
</header>

<main>
  <div class="warning">
    ⚠ 默认模式会在成功合并后删除源文件。首次使用请先点击“试运行”，或勾选“保留源文件”。
  </div>

  <section class="card">
    <h2>任务设置</h2>
    <div class="grid">
      <div class="field wide">
        <label for="config">配置文件 <span class="req">*</span></label>
        <div class="input-row">
          <input id="config" type="text" autocomplete="off" placeholder="/path/to/logreduce.toml">
          <button id="load-config" class="mini" type="button">读取配置</button>
        </div>
        <small>路径按启动 logreduce-gui 时的当前目录解析。</small>
      </div>

      <div class="field wide">
        <label for="log-dir">日志目录 <span class="req">*</span></label>
        <input id="log-dir" type="text" autocomplete="off" placeholder="/path/to/logs">
      </div>

      <div class="field wide">
        <label for="output">输出文件</label>
        <input id="output" type="text" autocomplete="off" placeholder="留空则使用配置中的 [output].path">
        <small>如果配置里没有 output.path，这里必须填写。</small>
      </div>

      <div class="field">
        <label for="mode">处理模式</label>
        <select id="mode">
          <option value="default">配置默认</option>
          <option value="inplace">inplace（原地处理）</option>
          <option value="direct">direct（直写合并，更快）</option>
        </select>
      </div>

      <div class="field">
        <label for="jobs">并行进程数</label>
        <input id="jobs" type="number" min="0" max="256" step="1" placeholder="留空则使用配置（0=自动）">
      </div>
    </div>

    <div class="options">
      <label><input id="keep-sources" type="checkbox"> 保留源文件</label>
      <label><input id="force" type="checkbox"> 覆盖已存在输出</label>
      <label><input id="no-fsync" type="checkbox"> 跳过 fsync</label>
      <label><input id="allow-changing" type="checkbox"> 允许文件变化</label>
      <label><input id="verbose" type="checkbox"> 详细进度</label>
    </div>

    <div class="actions">
      <button id="validate" class="ghost" type="button">校验配置</button>
      <button id="dry-run" class="ghost" type="button">试运行</button>
      <button id="run" class="primary" type="button">开始处理</button>
      <button id="stop" class="danger" type="button" disabled>停止</button>
    </div>
  </section>

  <section class="card">
    <div class="log-head">
      <h2>运行日志</h2>
      <label class="small"><input id="autoscroll" type="checkbox" checked> 自动滚动</label>
      <button id="clear-log" class="mini" type="button">清空</button>
    </div>
    <pre id="log" class="log" data-placeholder="1">等待任务…</pre>
    <div class="statusbar">
      <span id="status-text">就绪</span>
      <span id="elapsed"></span>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const logEl = $("log");
const statusPill = $("status-pill");
const statusText = $("status-text");
const elapsedEl = $("elapsed");
let since = 0;
let busy = false;
let pollTimer = null;
let pollFailures = 0;

function setStatus(kind, text) {
  statusPill.className = `pill ${kind}`;
  statusPill.textContent = text;
  statusText.textContent = text;
}

function setBusy(value) {
  busy = value;
  for (const id of ["validate", "dry-run", "run", "load-config"]) {
    $(id).disabled = value;
  }
  $("stop").disabled = !value;
}

function resetLog() {
  logEl.textContent = "";
  logEl.dataset.placeholder = "0";
  since = 0;
}

function appendLog(text) {
  if (logEl.dataset.placeholder === "1") {
    logEl.textContent = "";
    logEl.dataset.placeholder = "0";
  }
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 24;
  logEl.appendChild(document.createTextNode(text + "\n"));
  if ($("autoscroll").checked && atBottom) {
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function formatElapsed(seconds) {
  if (!seconds || seconds < 0) return "";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m} min ${s} s`;
}

function collect(action) {
  const config = $("config").value.trim();
  const logDir = $("log-dir").value.trim();
  const output = $("output").value.trim();
  const jobsRaw = $("jobs").value.trim();
  if (!config) throw new Error("请填写配置文件路径");
  if (action !== "validate" && !logDir) throw new Error("请填写日志目录");
  let jobs = null;
  if (jobsRaw !== "") {
    jobs = Number(jobsRaw);
    if (!Number.isInteger(jobs) || jobs < 0 || jobs > 256) {
      throw new Error("并行进程数必须是 0-256 的整数");
    }
  }
  return {
    action,
    config,
    log_dir: logDir,
    output,
    mode: $("mode").value,
    jobs,
    keep_sources: $("keep-sources").checked,
    force: $("force").checked,
    no_fsync: $("no-fsync").checked,
    allow_changing: $("allow-changing").checked,
    verbose: $("verbose").checked,
  };
}

async function parseResponse(resp) {
  let data = {};
  try { data = await resp.json(); } catch (e) { data = { error: resp.statusText }; }
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

async function startJob(action) {
  if (busy) return;
  let payload;
  try {
    payload = collect(action);
  } catch (e) {
    setStatus("error", "参数错误");
    if (logEl.dataset.placeholder === "1") resetLog();
    appendLog(`[参数错误] ${e.message}`);
    return;
  }
  resetLog();
  const label = action === "validate" ? "校验配置" : action === "dry-run" ? "试运行" : "开始处理";
  appendLog(`[启动] ${label}`);
  setBusy(true);
  setStatus("running", "运行中");
  elapsedEl.textContent = "";
  try {
    const data = await parseResponse(await fetch("/api/job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    if (data.command) appendLog(`[命令] ${data.command.join(" ")}`);
    if (pollTimer) clearTimeout(pollTimer);
    pollFailures = 0;
    pollTimer = setTimeout(poll, 150);
  } catch (e) {
    setBusy(false);
    setStatus("error", "启动失败");
    appendLog(`[启动失败] ${e.message}`);
  }
}

async function poll() {
  try {
    const data = await parseResponse(await fetch(`/api/status?since=${since}`, { cache: "no-store" }));
    pollFailures = 0;
    if (data.lines && data.lines.length) {
      for (const line of data.lines) appendLog(line);
    }
    since = data.next;
    elapsedEl.textContent = data.elapsed ? `耗时 ${formatElapsed(data.elapsed)}` : "";
    if (data.running) {
      setStatus("running", "运行中");
      pollTimer = setTimeout(poll, 400);
    } else {
      setBusy(false);
      if (data.error) setStatus("error", "失败");
      else if (data.returncode === 0) setStatus("ok", "完成");
      else setStatus("error", `失败（退出码 ${data.returncode}）`);
    }
  } catch (e) {
    pollFailures += 1;
    if (pollFailures === 1) appendLog(`[连接失败] ${e.message}`);
    if (busy && pollFailures < 5) {
      setStatus("error", "连接失败，重试中");
      $("stop").disabled = false;
      pollTimer = setTimeout(poll, 1000);
    } else {
      setBusy(false);
      setStatus("error", "连接失败");
    }
  }
}

async function stopJob() {
  try {
    const data = await parseResponse(await fetch("/api/stop", { method: "POST" }));
    appendLog(data.stopped ? "[停止] 已发送停止信号" : "[停止] 当前没有运行中的任务");
    if (data.stopped) {
      setStatus("running", "正在停止");
    } else if (busy) {
      setStatus("running", "运行中");
    } else {
      setStatus("idle", "空闲");
    }
  } catch (e) {
    appendLog(`[停止失败] ${e.message}`);
  }
}

async function loadConfig() {
  const path = $("config").value.trim();
  if (!path) {
    setStatus("error", "参数错误");
    if (logEl.dataset.placeholder === "1") resetLog();
    appendLog("[参数错误] 请先填写配置文件路径");
    return;
  }
  try {
    const query = new URLSearchParams({ path });
    const data = await parseResponse(await fetch(`/api/config?${query}`, { cache: "no-store" }));
    if (!$("output").value.trim() && data.output) $("output").value = data.output;
    if ($("mode").value === "default" && data.mode) $("mode").value = data.mode;
    if (!$("jobs").value.trim() && data.jobs > 0) $("jobs").value = String(data.jobs);
    if (logEl.dataset.placeholder === "1") resetLog();
    appendLog(`[配置] 有效：${data.rules} 条规则，模式 ${data.mode}，输出 ${data.output || "（未配置）"}`);
    appendLog(`[配置] include=${(data.include || []).join(", ")}`);
    appendLog(`[配置] 删除源文件=${data.delete_sources ? "是" : "否"}`);
    setStatus("ok", "配置有效");
  } catch (e) {
    setStatus("error", "配置错误");
    if (logEl.dataset.placeholder === "1") resetLog();
    appendLog(`[配置错误] ${e.message}`);
  }
}

$("validate").addEventListener("click", () => startJob("validate"));
$("dry-run").addEventListener("click", () => startJob("dry-run"));
$("run").addEventListener("click", () => startJob("run"));
$("stop").addEventListener("click", stopJob);
$("load-config").addEventListener("click", loadConfig);
$("clear-log").addEventListener("click", () => {
  logEl.textContent = "";
  logEl.dataset.placeholder = "0";
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
