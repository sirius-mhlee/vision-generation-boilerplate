"""Serialize state changes in a run directory and publish complete files by rename."""

import json
import os
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path


def image_output_path(edit_id: str, repeat_index: int) -> str:
    """Return the run-relative PNG path stored in a plan."""
    return f"images/r{repeat_index:03d}/{edit_id}.png"


def check_output_filenames(folder: Path, jobs: list[dict]) -> None:
    """Ask the destination filesystem whether each planned PNG name is usable."""
    directories = {}
    checked = set()
    for job in jobs:
        target = folder / job["output_path"]
        parent = target.parent
        if parent not in directories:
            # Repeat directories often do not exist yet. Probe the nearest existing parent.
            directories[parent] = next(path for path in target.parents if path.exists())
        probe = directories[parent] / target.name
        if probe in checked:
            continue
        checked.add(probe)
        try:
            probe.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            row = f"편집!B{job['sheet_row']} " if "sheet_row" in job else ""
            raise OSError(f"{row}결과 PNG 경로를 사용할 수 없습니다. {target} ({exc.strerror})") from exc


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
    # Writers hold the run lock. A retry can reclaim this target's staging file
    # even after a hard stop. Its short name never appears in a PNG file listing.
    key = sha256(os.fsencode(target.name)).hexdigest()
    path = target.with_name(f".vgen-{key}.tmp")
    path.unlink(missing_ok=True)
    path.touch(mode=0o600, exist_ok=False)
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
