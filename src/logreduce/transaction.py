from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .files import FileSystemError, SourceFile


_STATE_WORKING = "working"
_STATE_PUBLISHING = "publishing"
_STATE_PUBLISHED = "published"
_STATE_COMMITTED = "committed"
_TRANSACTION_PREFIX = ".logreduce-txn-"
_MANIFEST = "manifest.json"


class TransactionError(RuntimeError):
    """Raised when publishing or deleting files cannot complete safely."""


class Transaction:
    def __init__(self, root: Path, directory: Path, manifest: dict[str, Any]):
        self.root = root
        self.directory = directory
        self.manifest = manifest

    @classmethod
    def create(
        cls,
        root: Path,
        output_relative: str,
        sources: list[SourceFile],
    ) -> Transaction:
        directory = Path(tempfile.mkdtemp(prefix=_TRANSACTION_PREFIX, dir=root))
        manifest = {
            "version": 1,
            "state": _STATE_WORKING,
            "output": output_relative,
            "sources": [
                {
                    "relative_path": source.relative_path,
                    "size": source.size,
                    "mtime_ns": source.mtime_ns,
                    "inode": source.inode,
                    "device": source.device,
                }
                for source in sources
            ],
        }
        transaction = cls(root, directory, manifest)
        try:
            transaction.fragment_directory.mkdir()
            transaction.trash_directory.mkdir()
            transaction._write_manifest()
            _fsync_directory(root)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return transaction

    @property
    def fragment_directory(self) -> Path:
        return self.directory / "fragments"

    @property
    def trash_directory(self) -> Path:
        return self.directory / "trash"

    @property
    def final_path(self) -> Path:
        return self.directory / "final-output"

    @property
    def backup_path(self) -> Path:
        return self.directory / "previous-output"

    @property
    def output_path(self) -> Path:
        return self.root / self.manifest["output"]

    def commit(self, force: bool) -> None:
        output = self.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output_exists = _path_exists(output)
        if output_exists and not force:
            raise TransactionError(
                f"output file already exists (use --force to replace it): {output}"
            )
        if output_exists and (output.is_symlink() or not output.is_file()):
            raise TransactionError(f"output path is not a regular file: {output}")

        final_identity = _identity(self.final_path)
        old_identity = _identity(output) if output_exists else None
        self.manifest.update(
            {
                "state": _STATE_PUBLISHING,
                "had_output": output_exists,
                "final_identity": final_identity,
                "old_identity": old_identity,
            }
        )
        self._write_manifest()

        moved_sources: list[str] = []
        output_published = False
        try:
            if output_exists:
                os.replace(output, self.backup_path)
            os.replace(self.final_path, output)
            output_published = True
            _fsync_file(output)
            _fsync_directory(output.parent)

            self.manifest["state"] = _STATE_PUBLISHED
            self._write_manifest()

            for source_entry in self.manifest["sources"]:
                relative_path = source_entry["relative_path"]
                source_path = self.root / relative_path
                _verify_manifest_source(source_path, source_entry)
                destination = self.trash_directory / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source_path, destination)
                moved_sources.append(relative_path)

            self.manifest["state"] = _STATE_COMMITTED
            self._write_manifest()
        except BaseException as exc:
            self._rollback_commit(
                moved_sources=moved_sources,
                output_published=output_published,
                had_output=output_exists,
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise TransactionError(f"transaction commit failed: {exc}") from exc

        try:
            self._remove_transaction_data()
        except OSError as exc:
            raise TransactionError(
                f"output was committed, but transaction cleanup failed at "
                f"{self.directory}: {exc}"
            ) from exc

    def abort(self) -> None:
        try:
            _remove_working_transaction(self.directory)
        except OSError:
            # The durable journal lets the next invocation resume cleanup.
            pass

    def abort_if_working(self) -> None:
        """Discard staging only when no commit journal requires recovery."""
        manifest_path = self.directory / _MANIFEST
        try:
            with manifest_path.open("r", encoding="utf-8") as manifest_file:
                state = json.load(manifest_file).get("state")
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            return
        if state == _STATE_WORKING:
            self.abort()

    def _write_manifest(self) -> None:
        temporary = self.directory / ".manifest.tmp"
        with temporary.open("w", encoding="utf-8") as manifest_file:
            json.dump(
                self.manifest,
                manifest_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        os.replace(temporary, self.directory / _MANIFEST)
        _fsync_directory(self.directory)

    def _rollback_commit(
        self,
        moved_sources: list[str],
        output_published: bool,
        had_output: bool,
    ) -> None:
        rollback_errors: list[str] = []
        for relative_path in reversed(moved_sources):
            destination = self.trash_directory / relative_path
            source = self.root / relative_path
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if _path_exists(source):
                    raise FileExistsError(
                        f"source path was recreated during rollback: {relative_path}"
                    )
                os.replace(destination, source)
            except OSError as exc:
                rollback_errors.append(f"{relative_path}: {exc}")

        output = self.output_path
        try:
            if output_published and _identity(output) == self.manifest.get(
                "final_identity"
            ):
                output.unlink(missing_ok=True)
            if _path_exists(self.backup_path):
                os.replace(self.backup_path, output)
                _fsync_directory(output.parent)
        except OSError as exc:
            rollback_errors.append(f"output restoration: {exc}")

        if rollback_errors:
            raise TransactionError(
                "commit failed and rollback was incomplete; inspect "
                f"{self.directory}: {'; '.join(rollback_errors)}"
            )
        if not had_output:
            self.manifest["state"] = _STATE_WORKING
            try:
                self._write_manifest()
            except OSError:
                pass
        self.abort()

    def _remove_transaction_data(self) -> None:
        _remove_tree_if_present(self.trash_directory)
        _remove_tree_if_present(self.fragment_directory)
        for path in (
            self.final_path,
            self.backup_path,
            self.directory / ".manifest.tmp",
        ):
            path.unlink(missing_ok=True)
        (self.directory / _MANIFEST).unlink(missing_ok=True)
        self.directory.rmdir()
        _fsync_directory(self.root)


def recover_pending_transactions(root: Path) -> list[Path]:
    recovered: list[Path] = []
    try:
        candidates = sorted(
            path
            for path in root.iterdir()
            if path.name.startswith(_TRANSACTION_PREFIX) and path.is_dir()
        )
    except OSError as exc:
        raise TransactionError(f"cannot inspect recovery journals: {exc}") from exc

    for directory in candidates:
        manifest_path = directory / _MANIFEST
        if not manifest_path.is_file():
            _remove_orphan_transaction(directory)
            recovered.append(directory)
            continue
        try:
            with manifest_path.open("r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionError(
                f"cannot read transaction journal {manifest_path}: {exc}"
            ) from exc

        state = manifest.get("state")
        if state == _STATE_WORKING:
            _remove_working_transaction(directory)
        elif state == _STATE_PUBLISHING:
            _recover_publishing(root, directory, manifest)
        elif state in {_STATE_PUBLISHED, _STATE_COMMITTED}:
            _recover_published(root, directory, manifest)
        else:
            raise TransactionError(
                f"unknown transaction state {state!r} in {manifest_path}"
            )
        recovered.append(directory)
    return recovered


def pending_transaction_directories(root: Path) -> list[Path]:
    try:
        return sorted(
            path
            for path in root.iterdir()
            if path.name.startswith(_TRANSACTION_PREFIX) and path.is_dir()
        )
    except OSError as exc:
        raise TransactionError(f"cannot inspect recovery journals: {exc}") from exc


def _recover_publishing(
    root: Path,
    directory: Path,
    manifest: dict[str, Any],
) -> None:
    output = root / manifest["output"]
    backup = directory / "previous-output"
    output_identity = _identity(output) if _path_exists(output) else None
    final_identity = _stored_identity(manifest.get("final_identity"))
    old_identity = _stored_identity(manifest.get("old_identity"))
    try:
        if output_identity is not None and output_identity == final_identity:
            output.unlink()
            output_identity = None
        if _path_exists(backup):
            if output_identity is not None:
                raise TransactionError(
                    f"cannot restore previous output because {output} still exists"
                )
            os.replace(backup, output)
            _fsync_directory(output.parent)
        elif manifest.get("had_output") and output_identity != old_identity:
            raise TransactionError(
                f"cannot safely recover previous output for {output}"
            )
    except OSError as exc:
        raise TransactionError(f"failed to roll back interrupted publication: {exc}") from exc
    _remove_working_transaction(directory)


def _recover_published(
    root: Path,
    directory: Path,
    manifest: dict[str, Any],
) -> None:
    if manifest.get("state") == _STATE_COMMITTED:
        _remove_transaction_directory(directory)
        return

    output = root / manifest["output"]
    if not _path_exists(output) or _identity(output) != _stored_identity(
        manifest.get("final_identity")
    ):
        raise TransactionError(
            f"published transaction output is missing or changed: {output}"
        )

    trash = directory / "trash"
    for source_entry in manifest.get("sources", []):
        relative_path = source_entry["relative_path"]
        source = root / relative_path
        destination = trash / relative_path
        if _path_exists(source):
            if _path_exists(destination):
                raise TransactionError(
                    f"both source and staged copy exist: {relative_path}"
                )
            _verify_manifest_source(source, source_entry)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        elif not _path_exists(destination):
            raise TransactionError(
                f"source is missing from both original and transaction: {relative_path}"
            )
    _remove_transaction_directory(directory)


def _verify_manifest_source(path: Path, entry: dict[str, Any]) -> None:
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise FileSystemError(
            f"source disappeared before commit: {entry['relative_path']}: {exc}"
        ) from exc
    actual = (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )
    expected = (
        entry["device"],
        entry["inode"],
        entry["size"],
        entry["mtime_ns"],
    )
    if actual != expected:
        raise FileSystemError(
            f"source changed before commit: {entry['relative_path']}"
        )


def _remove_working_transaction(directory: Path) -> None:
    if not directory.exists():
        return
    for child in (
        directory / "trash",
        directory / "fragments",
        directory / "final-output",
        directory / "previous-output",
        directory / ".manifest.tmp",
        directory / _MANIFEST,
    ):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    directory.rmdir()


def _remove_transaction_directory(directory: Path) -> None:
    _remove_working_transaction(directory)


def _remove_orphan_transaction(directory: Path) -> None:
    nonempty = any(directory.iterdir())
    if nonempty:
        raise TransactionError(
            f"transaction directory is missing its journal: {directory}"
        )
    directory.rmdir()


def _remove_tree_if_present(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return None
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size)


def _stored_identity(value: Any) -> tuple[int, int, int] | None:
    if value is None:
        return None
    return (int(value[0]), int(value[1]), int(value[2]))


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
