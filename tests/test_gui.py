from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode

from logreduce.gui.config_document import (
    config_to_document,
    parse_content,
    serialize_config,
)
from logreduce.gui.jobs import JobBusyError, JobManager
from logreduce.gui.server import create_server


class GuiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = "test-token"
        self.server = create_server(token=self.token)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.jobs.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_index_embeds_token_and_requires_api_authentication(self) -> None:
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), body)
        self.assertIn("Content-Security-Policy", headers)

        status, _, body = self._request("GET", "/api/bootstrap", token=None)
        self.assertEqual(status, 403)
        self.assertIn("invalid GUI token", body.decode())

        status, _, body = self._request(
            "GET",
            "/api/bootstrap",
            extra_headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("untrusted Origin", body.decode())

    def test_directory_listing_config_load_validation_and_save(self) -> None:
        config_path = self.root / "reduce.toml"
        config_path.write_text(
            'version = 1\noutput = "out.log"\n',
            encoding="utf-8",
        )

        status, _, body = self._request(
            "GET",
            f"/api/fs?{urlencode({'path': str(self.root)})}",
        )
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(listing["path"], str(self.root))
        self.assertEqual(listing["configs"][0]["name"], "reduce.toml")

        status, _, body = self._request(
            "POST",
            "/api/config/load",
            {"path": str(config_path)},
        )
        self.assertEqual(status, 200)
        loaded = json.loads(body)
        self.assertTrue(loaded["valid"])
        self.assertEqual(loaded["document"]["output"], "out.log")

        invalid_document = loaded["document"]
        invalid_document["rules"] = [
            {
                "name": "bad",
                "filter": {"mode": "include", "pattern": "["},
                "replace": None,
            }
        ]
        status, _, body = self._request(
            "POST",
            "/api/config/validate",
            {"document": invalid_document},
        )
        self.assertEqual(status, 200)
        validation = json.loads(body)
        self.assertFalse(validation["valid"])
        self.assertIn("invalid regex", validation["error"])

        valid_document = loaded["document"]
        valid_document["rules"] = [
            {
                "name": "keep errors",
                "filter": {"mode": "include", "pattern": "ERROR"},
                "replace": None,
            }
        ]
        status, _, body = self._request(
            "POST",
            "/api/config/validate",
            {"document": valid_document},
        )
        self.assertEqual(status, 200)
        canonical = json.loads(body)["content"]
        self.assertIn("[[rules]]", canonical)

        destination = self.root / "saved.toml"
        status, _, body = self._request(
            "POST",
            "/api/config/save",
            {
                "path": str(destination),
                "overwrite": False,
                "document": valid_document,
            },
        )
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertTrue(saved["valid"])
        self.assertEqual(
            config_to_document(parse_content(destination.read_text(encoding="utf-8"))),
            saved["document"],
        )

        status, _, _ = self._request(
            "POST",
            "/api/config/save",
            {
                "path": str(destination),
                "overwrite": False,
                "content": canonical,
            },
        )
        self.assertEqual(status, 409)

    def test_dry_run_and_confirmed_real_job(self) -> None:
        source = self.root / "app.log"
        source.write_bytes(b"INFO ignored\nERROR kept\n")
        config = self.root / "reduce.toml"
        config.write_text(
            """\
version = 1
output = "combined.log"

[[rules]]
name = "errors"
[rules.filter]
mode = "include"
pattern = "ERROR"
""",
            encoding="utf-8",
        )
        common = {
            "log_dir": str(self.root),
            "config_path": str(config),
            "workers": 1,
            "force": False,
        }

        status, _, body = self._request(
            "POST",
            "/api/jobs",
            {**common, "dry_run": True, "acknowledge_deletion": False},
        )
        self.assertEqual(status, 202)
        dry_job = self._wait_for_job(json.loads(body)["id"])
        self.assertEqual(dry_job["state"], "succeeded", dry_job)
        self.assertTrue(dry_job["summary"]["dry_run"])
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "combined.log").exists())

        status, _, body = self._request(
            "POST",
            "/api/jobs",
            {**common, "dry_run": False, "acknowledge_deletion": False},
        )
        self.assertEqual(status, 400)
        self.assertIn("confirmation", body.decode())

        (self.root / "combined.log").write_bytes(b"previous\n")
        status, _, body = self._request(
            "POST",
            "/api/jobs",
            {**common, "dry_run": False, "acknowledge_deletion": True},
        )
        self.assertEqual(status, 202)
        failed_job = self._wait_for_job(json.loads(body)["id"])
        self.assertEqual(failed_job["state"], "failed", failed_job)
        self.assertIn("--force", failed_job["error"])
        self.assertTrue(source.exists())
        self.assertEqual(
            (self.root / "combined.log").read_bytes(),
            b"previous\n",
        )

        status, _, body = self._request(
            "POST",
            "/api/jobs",
            {
                **common,
                "force": True,
                "dry_run": False,
                "acknowledge_deletion": True,
            },
        )
        self.assertEqual(status, 202)
        real_job = self._wait_for_job(json.loads(body)["id"])
        self.assertEqual(real_job["state"], "succeeded", real_job)
        self.assertFalse(source.exists())
        self.assertEqual(
            (self.root / "combined.log").read_bytes(),
            b"ERROR kept\n",
        )
        self.assertEqual(real_job["completed_tasks"], 1)
        self.assertGreaterEqual(len(real_job["events"]), 2)

    def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, _, body = self._request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            if payload["state"] in {"succeeded", "failed", "canceled"}:
                return payload
            time.sleep(0.03)
        self.fail(f"job {job_id} did not finish")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        token: str | None = "__default__",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers: dict[str, str] = {}
        if token == "__default__":
            headers["X-LogReduce-Token"] = self.token
        elif token is not None:
            headers["X-LogReduce-Token"] = token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            return response.status, dict(response.getheaders()), response_body
        finally:
            connection.close()


class ConfigDocumentTests(unittest.TestCase):
    def test_round_trip_preserves_ordered_rules_and_escaped_strings(self) -> None:
        content = """\
version = 1
output = "nested/out.log"
encoding = "utf-8"

[[rules]]
name = "quotes \\" and slash \\\\"
[rules.filter]
mode = "exclude"
pattern = "health\\\\s+check"
[rules.replace]
pattern = "id=(\\\\d+)"
replacement = "id=<\\\\1>"
"""
        config = parse_content(content)
        serialized = serialize_config(config)
        reparsed = parse_content(serialized)
        self.assertEqual(config_to_document(reparsed), config_to_document(config))
        self.assertEqual(reparsed.rules[0].name, 'quotes " and slash \\')


class JobManagerTests(unittest.TestCase):
    def test_busy_job_can_be_canceled(self) -> None:
        released = threading.Event()

        class FakeStdout:
            def __iter__(self):
                released.wait(timeout=2)
                return iter(())

            def close(self) -> None:
                pass

        class FakeProcess:
            pid = 4242
            stdout = FakeStdout()

            def poll(self) -> None:
                return None

            def wait(self) -> int:
                return -15

        manager = JobManager()
        with (
            mock.patch("subprocess.Popen", return_value=FakeProcess()),
            mock.patch("os.killpg", side_effect=lambda *_: released.set()),
        ):
            job = manager.start(
                log_dir="/tmp/logs",
                config_path="/tmp/reduce.toml",
                workers=1,
                force=False,
                dry_run=True,
            )
            with self.assertRaises(JobBusyError):
                manager.start(
                    log_dir="/tmp/logs",
                    config_path="/tmp/reduce.toml",
                    workers=1,
                    force=False,
                    dry_run=True,
                )
            manager.cancel(job.job_id)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = job.snapshot()
            if snapshot["state"] == "canceled":
                break
            time.sleep(0.01)
        self.assertEqual(job.snapshot()["state"], "canceled")


if __name__ == "__main__":
    unittest.main()
