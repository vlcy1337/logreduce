from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..config import ConfigError, load_config
from ..engine import available_workers
from ..files import FileSystemError
from .config_document import (
    config_to_document,
    parse_content,
    parse_document,
    serialize_config,
)
from .jobs import JobBusyError, JobManager


_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ApiError(RuntimeError):
    def __init__(self, status: HTTPStatus, message: str, kind: str = "request"):
        super().__init__(message)
        self.status = status
        self.kind = kind


class GuiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        token: str,
        *,
        initial_log_dir: str | None = None,
        initial_config: str | None = None,
    ) -> None:
        super().__init__(server_address, GuiRequestHandler)
        self.token = token
        self.initial_log_dir = initial_log_dir
        self.initial_config = initial_config
        self.jobs = JobManager()


class GuiRequestHandler(BaseHTTPRequestHandler):
    server: GuiHTTPServer

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._serve_index()
                return
            if parsed.path.startswith("/static/"):
                self._serve_static(parsed.path.removeprefix("/static/"))
                return
            self._require_trusted_request()
            if parsed.path == "/api/bootstrap":
                self._send_json(self._bootstrap())
            elif parsed.path == "/api/fs":
                query = parse_qs(parsed.query)
                requested = query.get("path", [""])[0]
                self._send_json(_list_directory(requested))
            elif parsed.path.startswith("/api/jobs/"):
                self._get_job(parsed.path.removeprefix("/api/jobs/"))
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "API endpoint not found")
        except ApiError as exc:
            self._send_error(exc.status, str(exc), exc.kind)
        except (OSError, FileSystemError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc), "filesystem")
        except Exception as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"internal server error: {exc}",
                "internal",
            )

    def do_POST(self) -> None:
        try:
            self._require_trusted_request()
            payload = self._read_json()
            parsed = urlsplit(self.path)
            if parsed.path == "/api/config/load":
                self._send_json(self._load_config(payload))
            elif parsed.path == "/api/config/validate":
                self._send_json(self._validate_config(payload))
            elif parsed.path == "/api/config/save":
                self._send_json(self._save_config(payload))
            elif parsed.path == "/api/jobs":
                self._start_job(payload)
            elif (
                parsed.path.startswith("/api/jobs/")
                and parsed.path.endswith("/cancel")
            ):
                job_id = parsed.path.removeprefix("/api/jobs/").removesuffix(
                    "/cancel"
                )
                self._cancel_job(job_id)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "API endpoint not found")
        except ApiError as exc:
            self._send_error(exc.status, str(exc), exc.kind)
        except ConfigError as exc:
            self._send_error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), "configuration")
        except (OSError, FileSystemError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc), "filesystem")
        except JobBusyError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc), "busy")
        except Exception as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"internal server error: {exc}",
                "internal",
            )

    def _bootstrap(self) -> dict[str, Any]:
        cwd = Path.cwd().resolve()
        return {
            "cwd": str(cwd),
            "home": str(Path.home()),
            "workers": available_workers(),
            "initial_log_dir": self.server.initial_log_dir,
            "initial_config": self.server.initial_config,
        }

    def _load_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_existing_file(_required_string(payload, "path"))
        content = _read_config_text(path)
        try:
            config = parse_content(content)
        except ConfigError as exc:
            return {
                "path": str(path),
                "content": content,
                "valid": False,
                "error": str(exc),
                "document": None,
            }
        return {
            "path": str(path),
            "content": content,
            "valid": True,
            "error": None,
            "document": config_to_document(config),
        }

    def _validate_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content")
        document = payload.get("document")
        if (content is None) == (document is None):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "provide exactly one of content or document",
            )
        try:
            if content is not None:
                if not isinstance(content, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "content must be a string")
                config = parse_content(content)
                canonical_content = content
            else:
                config = parse_document(document)
                canonical_content = serialize_config(config)
        except ConfigError as exc:
            return {
                "valid": False,
                "error": str(exc),
                "document": None,
                "content": None,
            }
        return {
            "valid": True,
            "error": None,
            "document": config_to_document(config),
            "content": canonical_content,
        }

    def _save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_path = _required_string(payload, "path")
        overwrite = payload.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "overwrite must be a boolean")
        content = payload.get("content")
        document = payload.get("document")
        if (content is None) == (document is None):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "provide exactly one of content or document",
            )
        if content is not None:
            if not isinstance(content, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "content must be a string")
            config = parse_content(content)
            output_content = content
        else:
            config = parse_document(document)
            output_content = serialize_config(config)
        if len(output_content.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "configuration file is too large",
                "configuration",
            )

        path = _resolve_save_path(requested_path)
        if path.exists() and not overwrite:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"configuration already exists: {path}",
                "conflict",
            )
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"configuration path is not a regular file: {path}",
                "filesystem",
            )
        _atomic_write(path, output_content)
        return {
            "path": str(path),
            "content": output_content,
            "valid": True,
            "document": config_to_document(config),
        }

    def _start_job(self, payload: dict[str, Any]) -> None:
        log_dir = _resolve_existing_directory(
            _required_string(payload, "log_dir")
        )
        config_path = _resolve_existing_file(
            _required_string(payload, "config_path")
        )
        load_config(config_path)
        force = _optional_bool(payload, "force", False)
        dry_run = _optional_bool(payload, "dry_run", False)
        acknowledged = _optional_bool(
            payload,
            "acknowledge_deletion",
            False,
        )
        if not dry_run and not acknowledged:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "deletion confirmation is required for a real run",
                "confirmation",
            )
        workers = _optional_positive_integer(payload.get("workers"))
        job = self.server.jobs.start(
            log_dir=str(log_dir),
            config_path=str(config_path),
            workers=workers,
            force=force,
            dry_run=dry_run,
        )
        self._send_json(
            job.snapshot(),
            status=HTTPStatus.ACCEPTED,
        )

    def _get_job(self, job_id: str) -> None:
        job = self.server.jobs.get(job_id)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "job not found", "not_found")
        self._send_json(job.snapshot())

    def _cancel_job(self, job_id: str) -> None:
        job = self.server.jobs.cancel(job_id)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "job not found", "not_found")
        self._send_json(job.snapshot(), status=HTTPStatus.ACCEPTED)

    def _serve_index(self) -> None:
        resource = files("logreduce.gui").joinpath("static", "index.html")
        content = resource.read_bytes().replace(
            b"__LOGREDUCE_TOKEN__",
            self.server.token.encode("ascii"),
        )
        self._send_bytes(
            HTTPStatus.OK,
            content,
            "text/html; charset=utf-8",
        )

    def _serve_static(self, filename: str) -> None:
        content_type = _STATIC_FILES.get(filename)
        if content_type is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "static resource not found")
        try:
            content = (
                files("logreduce.gui")
                .joinpath("static", filename)
                .read_bytes()
            )
        except (FileNotFoundError, OSError) as exc:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "static resource not found",
            ) from exc
        self._send_bytes(HTTPStatus.OK, content, content_type)

    def _require_trusted_request(self) -> None:
        host = self.headers.get("Host", "")
        host_name, host_port = _split_host(host)
        if host_name not in {"127.0.0.1", "localhost"} or host_port != str(
            self.server.server_address[1]
        ):
            raise ApiError(HTTPStatus.FORBIDDEN, "untrusted Host header", "auth")

        token = self.headers.get("X-LogReduce-Token", "")
        if not secrets.compare_digest(token, self.server.token):
            raise ApiError(HTTPStatus.FORBIDDEN, "invalid GUI token", "auth")

        origin = self.headers.get("Origin")
        if origin is not None:
            parsed_origin = urlsplit(origin)
            expected_port = str(self.server.server_address[1])
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname not in {"127.0.0.1", "localhost"}
                or str(parsed_origin.port or 80) != expected_port
            ):
                raise ApiError(HTTPStatus.FORBIDDEN, "untrusted Origin header", "auth")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid Content-Length",
            ) from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request is too large")
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return payload

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: HTTPStatus, message: str, kind: str) -> None:
        self._send_json(
            {"error": {"message": message, "kind": kind}},
            status=status,
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"logreduce-gui: {self.address_string()} - {format % args}",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logreduce-gui",
        description="Start the local LogReduce browser interface.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--port",
        type=_port_number,
        default=0,
        metavar="N",
        help="local TCP port (default: choose an available port)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL without opening a browser",
    )
    parser.add_argument("--log-dir", metavar="PATH", help="initial log directory")
    parser.add_argument("--config", metavar="PATH", help="initial TOML configuration")
    return parser


def create_server(
    *,
    port: int = 0,
    token: str | None = None,
    initial_log_dir: str | None = None,
    initial_config: str | None = None,
) -> GuiHTTPServer:
    return GuiHTTPServer(
        ("127.0.0.1", port),
        token or secrets.token_urlsafe(32),
        initial_log_dir=initial_log_dir,
        initial_config=initial_config,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = create_server(
            port=args.port,
            initial_log_dir=args.log_dir,
            initial_config=args.config,
        )
    except OSError as exc:
        print(f"logreduce-gui: cannot start local server: {exc}", file=sys.stderr)
        return 1
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"LogReduce GUI 已启动：{url}", flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n正在停止 LogReduce GUI。", flush=True)
    finally:
        server.jobs.shutdown()
        server.server_close()
    return 0


def _list_directory(requested: str) -> dict[str, Any]:
    path = _resolve_existing_directory(requested or str(Path.cwd()))
    directories: list[dict[str, str]] = []
    configs: list[dict[str, str]] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(
                            {"name": entry.name, "path": str(Path(entry.path))}
                        )
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and entry.name.lower().endswith(".toml")
                    ):
                        configs.append(
                            {"name": entry.name, "path": str(Path(entry.path))}
                        )
                except OSError:
                    continue
    except OSError as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"cannot list directory: {exc}",
            "filesystem",
        ) from exc
    directories.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    configs.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "parent": str(parent) if parent is not None else None,
        "directories": directories,
        "configs": configs,
    }


def _resolve_existing_directory(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"cannot resolve directory {path}: {exc}",
            "filesystem",
        ) from exc
    if not resolved.is_dir():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"path is not a directory: {resolved}",
            "filesystem",
        )
    return resolved


def _resolve_existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"cannot resolve file {path}: {exc}",
            "filesystem",
        ) from exc
    if not resolved.is_file():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"path is not a file: {resolved}",
            "filesystem",
        )
    return resolved


def _resolve_save_path(value: str) -> Path:
    if not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "configuration path is required")
    requested = Path(value).expanduser()
    if requested.name in {"", ".", ".."}:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "configuration path must name a file",
        )
    relative = requested if requested.is_absolute() else Path.cwd() / requested
    parent = relative.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"configuration parent is not a directory: {parent}",
        )
    return parent / relative.name


def _read_config_text(path: Path) -> str:
    if path.stat().st_size > _MAX_CONFIG_BYTES:
        raise ApiError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "configuration file is too large",
            "configuration",
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration must be UTF-8: {exc}") from exc


def _atomic_write(path: Path, content: str) -> None:
    existing_mode = (
        stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), existing_mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a non-empty string")
    return value


def _optional_bool(
    payload: dict[str, Any],
    key: str,
    default: bool,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} must be a boolean")
    return value


def _optional_positive_integer(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "workers must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "workers must be a positive integer",
        ) from exc
    if parsed < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "workers must be a positive integer")
    return parsed


def _port_number(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def _split_host(value: str) -> tuple[str, str | None]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return value, None
        host = value[1:closing]
        port = value[closing + 2 :] if value[closing + 1 :].startswith(":") else None
        return host, port
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, port
    return value, None


if __name__ == "__main__":
    raise SystemExit(main())
