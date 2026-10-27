from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from logreduce.errors import ConfigError
from logreduce.gui import (
    JobManager,
    LogBuffer,
    build_command,
    config_summary,
    create_server,
)


def write_config(path: Path, output: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[input]",
                'include = ["**/*.log"]',
                "[output]",
                f"path = {json.dumps(str(output))}",
                'separator = "\\n"',
                "delete_sources = true",
                "[engine]",
                'mode = "inplace"',
                "jobs = 1",
                'chunk_size = "4KiB"',
                "fsync = false",
                "[[rules]]",
                'action = "replace"',
                'match_type = "literal"',
                'pattern = "old"',
                'replacement = "new"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class CommandTests(unittest.TestCase):
    def test_validate_command(self):
        command = build_command({"action": "validate", "config": "app.toml"})
        self.assertEqual(
            command,
            [sys.executable, "-m", "logreduce", "--validate", "-c", "app.toml"],
        )

    def test_run_command_with_options(self):
        command = build_command(
            {
                "action": "run",
                "config": "app.toml",
                "log_dir": "logs",
                "output": "out.log",
                "mode": "direct",
                "jobs": 8,
                "keep_sources": True,
                "force": True,
                "no_fsync": True,
                "allow_changing": True,
                "verbose": True,
            }
        )
        self.assertEqual(command[:3], [sys.executable, "-m", "logreduce"])
        self.assertEqual(
            command[3:],
            [
                "-d",
                "logs",
                "-c",
                "app.toml",
                "-o",
                "out.log",
                "--mode",
                "direct",
                "--jobs",
                "8",
                "--keep-sources",
                "--force",
                "--no-fsync",
                "--allow-changing",
                "--verbose",
                "--stats",
            ],
        )

    def test_dry_run_command(self):
        command = build_command(
            {
                "action": "dry-run",
                "config": "app.toml",
                "log_dir": "logs",
            }
        )
        self.assertIn("--dry-run", command)
        self.assertIn("--stats", command)

    def test_defaults_omit_overrides(self):
        command = build_command(
            {"action": "run", "config": "app.toml", "log_dir": "logs"}
        )
        self.assertNotIn("--mode", command)
        self.assertNotIn("--jobs", command)
        self.assertNotIn("-o", command)
        self.assertEqual(command[-1], "--stats")

    def test_invalid_requests(self):
        cases = [
            {"action": "wat", "config": "a.toml"},
            {"action": "run", "log_dir": "logs"},
            {"action": "run", "config": "a.toml"},
            {"action": "run", "config": "a.toml", "log_dir": "logs", "mode": "x"},
            {"action": "run", "config": "a.toml", "log_dir": "logs", "jobs": -1},
            {"action": "run", "config": "a.toml", "log_dir": "logs", "jobs": 257},
            {"action": "run", "config": "a.toml", "log_dir": "logs", "jobs": 1.5},
            {"action": "run", "config": "a.toml", "log_dir": "logs", "force": "x"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    build_command(payload)

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app.toml"
            output = root / "out.log"
            write_config(config, output)
            summary = config_summary(str(config))
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["mode"], "inplace")
            self.assertEqual(summary["jobs"], 1)
            self.assertEqual(summary["rules"], 1)
            self.assertEqual(summary["output"], str(output))

            with self.assertRaises(ConfigError):
                config_summary(str(root / "missing.toml"))


class LogBufferTests(unittest.TestCase):
    def test_absolute_indexes_and_eviction(self):
        buffer = LogBuffer(max_lines=3)
        for line in ("1", "2", "3", "4", "5"):
            buffer.append(line)
        lines, next_index = buffer.since(0)
        self.assertEqual(lines, ["3", "4", "5"])
        self.assertEqual(next_index, 5)
        lines, next_index = buffer.since(3)
        self.assertEqual(lines, ["4", "5"])
        self.assertEqual(next_index, 5)
        lines, next_index = buffer.since(5)
        self.assertEqual(lines, [])
        self.assertEqual(next_index, 5)

    def test_clear(self):
        buffer = LogBuffer(max_lines=2)
        buffer.append("a")
        buffer.clear()
        buffer.append("b")
        self.assertEqual(buffer.since(0), (["b"], 1))


def wait_manager(manager: JobManager, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = manager.snapshot(0)
        if not data["running"]:
            return data
        time.sleep(0.02)
    raise AssertionError("job did not finish")


class JobManagerTests(unittest.TestCase):
    def test_capture_output(self):
        manager = JobManager()
        manager.start(
            [sys.executable, "-c", "print('hello'); print('world')"]
        )
        data = wait_manager(manager)
        self.assertEqual(data["returncode"], 0)
        self.assertIn("hello", data["lines"])
        self.assertIn("world", data["lines"])
        self.assertIn("[任务完成]", data["lines"])

    def test_reject_second_job_and_stop(self):
        manager = JobManager()
        manager.start([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            with self.assertRaises(RuntimeError):
                manager.start([sys.executable, "-c", "print('x')"])
            self.assertTrue(manager.stop())
            manager.wait_stopped(5)
            data = manager.snapshot(0)
            self.assertFalse(data["running"])
            self.assertIsNotNone(data["returncode"])
        finally:
            manager.stop()
            manager.wait_stopped(5)

    def test_start_failure_is_reported(self):
        manager = JobManager()
        with self.assertRaises(OSError):
            manager.start(["/definitely/not/a/logreduce-binary"])
        data = manager.snapshot(0)
        self.assertFalse(data["running"])
        self.assertEqual(data["returncode"], -1)
        self.assertTrue(any("无法启动进程" in line for line in data["lines"]))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        (self.logs / "a.log").write_bytes(b"old\nDEBUG\n")
        self.output = self.root / "merged.log"
        self.config = self.root / "config.toml"
        write_config(self.config, self.output)

        self.manager = JobManager()
        self.server = create_server("127.0.0.1", 0, self.manager)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.manager.wait_stopped, 5)
        self.addCleanup(self.manager.stop)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urlopen(self.base + "/api/health", timeout=1):
                    return
            except OSError:
                time.sleep(0.02)
        self.fail("server did not start")

    def _get_json(self, path: str) -> tuple[int, dict]:
        request = Request(self.base + path, method="GET")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def _post_raw(self, path: str, data: bytes) -> tuple[int, dict]:
        request = Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def _wait_job(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        since = 0
        all_lines: list[str] = []
        while time.monotonic() < deadline:
            status, data = self._get_json(f"/api/status?since={since}")
            self.assertEqual(status, 200)
            all_lines.extend(data["lines"])
            since = data["next"]
            if not data["running"]:
                data["lines"] = all_lines
                return data
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_index_and_health(self):
        with urlopen(self.base + "/", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("logReduce", body)
        self.assertIn("配置文件", body)
        status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, data = self._post_json("/api/stop", {})
        self.assertEqual(status, 200)
        self.assertFalse(data["stopped"])

    def test_validate_job(self):
        status, data = self._post_json(
            "/api/job", {"action": "validate", "config": str(self.config)}
        )
        self.assertEqual(status, 200, data)
        result = self._wait_job()
        self.assertEqual(result["returncode"], 0)
        self.assertIn("配置有效", "\n".join(result["lines"]))

    def test_dry_run_job(self):
        status, data = self._post_json(
            "/api/job",
            {
                "action": "dry-run",
                "config": str(self.config),
                "log_dir": str(self.logs),
            },
        )
        self.assertEqual(status, 200, data)
        result = self._wait_job()
        self.assertEqual(result["returncode"], 0)
        self.assertTrue((self.logs / "a.log").exists())
        self.assertFalse(self.output.exists())
        self.assertIn("dry-run", "\n".join(result["lines"]))

    def test_run_job_keep_sources(self):
        status, data = self._post_json(
            "/api/job",
            {
                "action": "run",
                "config": str(self.config),
                "log_dir": str(self.logs),
                "output": str(self.output),
                "jobs": 1,
                "keep_sources": True,
            },
        )
        self.assertEqual(status, 200, data)
        result = self._wait_job()
        self.assertEqual(result["returncode"], 0, "\n".join(result["lines"]))
        self.assertEqual(self.output.read_bytes(), b"new\nDEBUG\n")
        self.assertTrue((self.logs / "a.log").exists())

    def test_config_summary_endpoint(self):
        query = urlencode({"path": str(self.config)})
        status, data = self._get_json("/api/config?" + query)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["mode"], "inplace")
        self.assertEqual(data["rules"], 1)

    def test_invalid_requests(self):
        status, data = self._post_json("/api/job", {})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        status, data = self._post_raw("/api/job", b"not-json")
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        status, data = self._get_json("/api/status?since=abc")
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        status, data = self._get_json("/api/config?path=/nonexistent/config.toml")
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
