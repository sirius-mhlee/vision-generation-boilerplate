"""Continue a started run from its frozen plan, manifest and verified outputs."""

import json
import re
from hashlib import file_digest
from pathlib import Path, PurePosixPath

from PIL import Image

from .generate import (
    JOB_FIELDS, GenerateError, GenerationResult, load_renderer, read_plan, run_jobs, verify_inputs,
)
from .model_files import file_stamp
from .run_files import run_lock, started


def _read_run(folder: Path, plan: dict, plan_hash: str) -> list[dict]:
    path = folder / "run.json"
    try:
        run = json.loads(path.read_bytes())
        if run["schema_version"] != 1 or run["plan_sha256"] != plan_hash:
            raise ValueError(f"첫 실행의 계획 지문과 일치하는 plan.json이 필요합니다. {folder / 'plan.json'}")
        if run["model_dir"] != plan["model_dir"] or run["generation"] != plan["generation"]:
            raise ValueError("처음 기록한 모델 경로 또는 생성 설정이 계획과 다릅니다.")
        files = run["model_files"]
        if not isinstance(files, list):
            raise ValueError("모델 파일 목록을 확인하세요.")
        names = set()
        for item in files:
            name = item["path"]
            relative = PurePosixPath(name)
            if (not relative.parts or relative.is_absolute() or ".." in relative.parts
                    or name in names or not re.fullmatch("[0-9a-f]{64}", item["sha256"])):
                raise ValueError(f"모델 파일의 경로 또는 SHA-256을 확인하세요. {name}")
            names.add(name)
        return files
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise GenerateError(f"첫 실행 기록을 확인할 수 없습니다. {path} ({exc})") from exc


def _read_history(path: Path, jobs: list[dict]) -> tuple[dict[str, str], int | None, bool]:
    """Return the latest successful hashes and the append point after any torn last line."""
    by_output = {job["output_path"]: job for job in jobs}
    successes = {}
    truncate_at = None
    needs_newline = False
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GenerateError(f"실행 폴더 안의 작업 기록 파일을 확인하세요. {path}")
    if not path.exists():
        return successes, truncate_at, needs_newline
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            try:
                entry = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if not raw.endswith(b"\n"):
                    truncate_at = stream.tell() - len(raw)
                    break
                raise GenerateError(f"작업 기록을 읽을 수 없습니다. {path} {number}행 ({exc})") from exc
            try:
                output = entry["output_path"]
                job = by_output[output]
                if any(entry[key] != job[key] for key in JOB_FIELDS):
                    raise ValueError("기록된 작업이 계획과 다릅니다.")
                if entry["status"] in ("failed", "repairing"):
                    successes.pop(output, None)
                elif entry["status"] == "success" and re.fullmatch("[0-9a-f]{64}", entry["output_sha256"]):
                    successes[output] = entry["output_sha256"]
                else:
                    raise ValueError("작업 상태 또는 출력 SHA-256을 확인하세요.")
            except (KeyError, TypeError, ValueError) as exc:
                raise GenerateError(f"작업 기록을 확인하세요. {path} {number}행 ({exc})") from exc
            needs_newline = not raw.endswith(b"\n")
    return successes, truncate_at, needs_newline


def _matches_png(path: Path, expected: str | None) -> bool:
    if expected is None or path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != expected:
            return False
        stream.seek(0)
        try:
            with Image.open(stream, formats=("PNG",)) as image:
                image.verify()
        except (OSError, EOFError, SyntaxError, ValueError, Image.DecompressionBombError):
            return False
    return True


def resume(run_dir: str | Path, *, load_backend=None, report=None) -> GenerationResult:
    """Skip verified successes and append results for pending jobs under the run lock."""
    folder = Path(run_dir)
    if not folder.is_dir():
        raise GenerateError(f"준비된 실행 폴더를 지정하세요. vgen prepare <엑셀> --out <실행 폴더>: {folder}")
    try:
        with run_lock(folder):
            has_started = started(folder)
            if not has_started and (folder / "plan.json").is_file():
                raise GenerateError(f'준비만 된 실행입니다. 다음 명령으로 생성을 시작하세요. vgen generate "{folder}"')
            plan, plan_hash = read_plan(folder, allow_existing=has_started)
            model_files = _read_run(folder, plan, plan_hash)
            journal = folder / "manifest.jsonl"
            jobs = plan["jobs"]
            original_stamp = file_stamp(journal) if journal.exists() else None
            successes, truncate_at, needs_newline = _read_history(journal, jobs)
            pending = []
            for job in jobs:
                output = job["output_path"]
                if not _matches_png(folder / output, successes.get(output)):
                    pending.append(job)
            skipped = len(jobs) - len(pending)
            if not pending:
                return GenerationResult(0, 0, folder / "images", skipped)

            verify_inputs(pending)
            if report:
                report(f"기존 PNG {skipped}장 확인 · 재개할 작업 {len(pending)}건 · 로컬 모델 확인 중")
            render, _ = load_renderer(folder, plan, plan_hash, load_backend, expected_files=model_files)
            current_stamp = file_stamp(journal) if journal.exists() else None
            if current_stamp != original_stamp:
                raise GenerateError(f"재개 준비 중 작업 기록이 변경됐습니다. {journal}")
            with journal.open("a+b") as stream:
                # A killed writer may have left part of its last JSON object.
                if truncate_at is not None:
                    stream.truncate(truncate_at)
                if needs_newline:
                    stream.write(b"\n")
                return run_jobs(folder, pending, plan["generation"], render, stream, report,
                                resuming=True, skipped=skipped, prior_successes=successes)
    except OSError as exc:
        raise GenerateError(f"실행 폴더를 읽거나 기록할 수 없습니다. {folder} ({exc})") from exc
