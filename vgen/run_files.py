"""Serialize state changes in a run directory and publish complete files by rename."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


@contextmanager
def run_lock(folder: Path):
    # Lock the directory inode, which survives atomic replacement of plan.json.
    if os.name == "posix":
        import fcntl

        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    else:
        import msvcrt

        with (folder / ".vgen.lock").open("a+b") as stream:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def started(folder: Path) -> bool:
    return any(os.path.lexists(folder / name) for name in ("run.json", "manifest.jsonl"))


@contextmanager
def temporary_output(target: Path):
    with NamedTemporaryFile(dir=target.parent, prefix=f".{target.stem}-",
                            suffix=target.suffix, delete=False) as stream:
        path = Path(stream.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def write_json(target: Path, value: dict) -> None:
    with temporary_output(target) as temporary:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
