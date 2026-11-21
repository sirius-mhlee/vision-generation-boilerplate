"""Execute the frozen jobs in a prepared run, one verified PNG at a time."""

import json
import math
import os
import re
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path, PureWindowsPath

from PIL import Image

from .model_files import check_model_files
from .run_files import run_lock, started, temporary_output, write_json

JOB_FIELDS = ("display_number", "edit_id", "repeat_index", "seed", "inputs", "output_path")


class GenerateError(ValueError):
    """The run cannot continue with the supplied inputs or state."""


class InputChanged(GenerateError):
    """The bytes at an input path no longer match this plan."""


@dataclass(frozen=True)
class GenerationResult:
    succeeded: int
    failed: int
    image_dir: Path
    skipped: int = 0


def _check_job(job: dict, folder: Path, repeats: int) -> Path:
    edit_id = job["edit_id"]
    index = job["repeat_index"]
    if (not isinstance(edit_id, str) or not edit_id.strip() or edit_id in (".", "..")
            or PureWindowsPath(edit_id).name != edit_id or type(index) is not int
            or not 1 <= index <= repeats):
        raise ValueError("편집 ID 또는 반복 번호가 잘못됐습니다.")
    if type(job["seed"]) is not int or not 0 <= job["seed"] < 2**64:
        raise ValueError(f"{edit_id}: 작업 시드가 잘못됐습니다.")
    if not isinstance(job["model_prompt"], str) or not job["model_prompt"].strip():
        raise ValueError(f"{edit_id}: 모델 프롬프트가 비어 있습니다.")
    if type(job["display_number"]) is not int:
        raise ValueError(f"{edit_id}: 엑셀 번호를 확인하세요.")
    expected = f"images/r{index:03d}/{edit_id}.png"
    target = folder / expected
    if job["output_path"] != expected or not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError(f"{edit_id}: 출력 경로를 확인하세요. {job['output_path']}")
    inputs = job["inputs"]
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 3:
        raise ValueError(f"{edit_id}: 가이드와 레퍼런스 개수를 확인하세요.")
    last_slot = 0
    for position, item in enumerate(inputs, 1):
        if (item["position"] != position or not Path(item["path"]).is_absolute()
                or not re.fullmatch("[0-9a-f]{64}", item["sha256"])):
            raise ValueError(f"{edit_id}: {position}번째 입력 경로·순서·SHA-256을 확인하세요.")
        if position == 1:
            if item["role"] != "guide":
                raise ValueError(f"{edit_id}: 첫 입력은 가이드여야 합니다.")
        else:
            slot = item["reference_slot"]
            if item["role"] != "reference" or type(slot) is not int or not last_slot < slot <= 2:
                raise ValueError(f"{edit_id}: 레퍼런스 순서가 잘못됐습니다.")
            last_slot = slot
    return target


def read_plan(folder: Path, *, allow_existing: bool = False) -> tuple[dict, str]:
    path = folder / "plan.json"
    if not path.is_file():
        if allow_existing:
            raise GenerateError(f"첫 실행의 계획 파일을 복원하세요. {path}")
        raise GenerateError(f"{path}가 없습니다. vgen prepare <엑셀> --out <실행 폴더>를 먼저 실행하세요.")
    try:
        raw = path.read_bytes()
        plan = json.loads(raw)
        if plan["schema_version"] != 1 or not Path(plan["model_dir"]).is_absolute():
            raise ValueError("계획 형식 또는 모델 경로를 확인하세요.")
        jobs = plan["jobs"]
        if (not isinstance(jobs, list) or not jobs or type(plan["repeat"]) is not int
                or plan["repeat"] < 1 or type(plan["edit_count"]) is not int
                or len(jobs) != plan["job_count"] or len(jobs) != plan["edit_count"] * plan["repeat"]):
            raise ValueError("계획의 작업 수와 반복 수가 일치하지 않습니다.")
        opts = plan["generation"]
        if (type(opts["num_inference_steps"]) is not int or opts["num_inference_steps"] < 1
                or type(opts["max_edge"]) is not int or not 16 <= opts["max_edge"] <= 1024
                or not math.isfinite(opts["guidance_scale"]) or opts["png_compress_level"] != 0):
            raise ValueError("생성 옵션을 확인하세요.")
        targets = set()
        for job in jobs:
            target = _check_job(job, folder, plan["repeat"])
            resolved = target.resolve()
            if resolved in targets or (not allow_existing and os.path.lexists(target)):
                raise ValueError(f"중복되거나 이미 존재하는 출력 파일입니다. {target}")
            targets.add(resolved)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        advice = "첫 실행에서 저장한 계획과 출력 경로를 확인하세요." if allow_existing else "vgen prepare로 계획을 확인하세요."
        raise GenerateError(f"{path}: {exc}. {advice}") from exc
    return plan, sha256(raw).hexdigest()


def _input_bytes(item: dict) -> bytes:
    path = Path(item["path"])
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InputChanged(f"입력 PNG를 읽을 수 없습니다. {path} ({exc})") from exc
    actual = sha256(data).hexdigest()
    if actual != item["sha256"]:
        raise InputChanged(f"입력 PNG가 변경됐습니다. {path} (계획 {item['sha256']}, 현재 {actual})")
    return data


def _append(stream, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    stream.write(line.encode("utf-8"))
    stream.flush()
    os.fsync(stream.fileno())


def _execute_job(folder, job, generation, render, manifest, *, replace_existing, had_success):
    record = {key: job[key] for key in JOB_FIELDS}
    record.update(status="failed", output_sha256=None)
    try:
        sources = [_input_bytes(item) for item in job["inputs"]]
    except InputChanged as exc:
        _append(manifest, {**record, "error": str(exc)})
        raise
    if had_success:
        # A deterministic repair may reproduce the old hash. Retire that success
        # durably before a replacement can appear at the same path.
        _append(manifest, {**record, "status": "repairing"})
    target = folder / job["output_path"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary_output(target) as temporary:
            metrics = render(sources, prompt=job["model_prompt"], seed=job["seed"],
                             generation=generation, output=temporary)
            with Image.open(temporary, formats=("PNG",)) as image:
                image.verify()
            with Image.open(temporary, formats=("PNG",)) as image:
                image.load()
            with temporary.open("r+b") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
                os.fsync(stream.fileno())
            if not replace_existing and os.path.lexists(target):
                raise FileExistsError(f"이미 출력 파일이 있습니다. {target}")
            os.replace(temporary, target)
        record.update(status="success", output_sha256=digest, metrics=metrics)
    except Exception as exc:
        # A failed edit is independent of later rows. Interrupts still propagate.
        record["error"] = f"{type(exc).__name__}: {exc}"
    _append(manifest, record)
    return record


def _require_new_run(folder: Path) -> None:
    if started(folder):
        raise GenerateError(
            f"이미 생성이 시작된 폴더입니다. {folder}\n"
            f"기존 실행은 vgen resume \"{folder}\"으로 이어가세요. "
            "다시 생성하려면 vgen prepare의 --out에 새 경로를 지정하세요."
        )


def verify_inputs(jobs: list[dict]) -> None:
    verified = set()
    for job in jobs:
        for item in job["inputs"]:
            key = item["path"], item["sha256"]
            if key not in verified:
                _input_bytes(item)
                verified.add(key)


def load_renderer(folder, plan, plan_hash, load_backend, *, expected_files=None):
    directory = Path(plan["model_dir"])
    try:
        if load_backend is None:
            from .flux import open_flux
            render, metadata = open_flux(directory, expected_files=expected_files)
        else:
            render, metadata = load_backend(directory)
            if expected_files is not None:
                check_model_files(directory, expected_files, metadata["model_files"])
    except ImportError as exc:
        raise GenerateError(f"README의 설치 절차에서 GPU 추론 패키지를 확인하세요. ({exc})") from exc
    except Exception as exc:
        raise GenerateError(f"로컬 모델을 준비할 수 없습니다. {directory} ({exc})") from exc
    if sha256((folder / "plan.json").read_bytes()).hexdigest() != plan_hash:
        raise GenerateError(f"모델 적재 중 계획이 변경됐습니다. {folder / 'plan.json'}")
    return render, metadata


def run_jobs(folder, jobs, generation, render, manifest, report, *, resuming=False, skipped=0,
             prior_successes=()):
    """A scheduled path in prior_successes has a recorded result that failed verification."""
    succeeded = 0
    for n, job in enumerate(jobs, 1):
        entry = _execute_job(folder, job, generation, render, manifest, replace_existing=resuming,
                             had_success=job["output_path"] in prior_successes)
        if entry["status"] == "success":
            succeeded += 1
        if report:
            result = entry.get("error", "저장 완료")
            report(f"[{n}/{len(jobs)}] {job['output_path']} · {result}")
    return GenerationResult(succeeded, len(jobs) - succeeded, folder / "images", skipped)


def generate(run_dir: str | Path, *, load_backend=None, report=None) -> GenerationResult:
    """Run a saved plan. The loader returns an image renderer and run metadata."""
    folder = Path(run_dir)
    if not folder.is_dir():
        raise GenerateError(f"준비된 실행 폴더를 지정하세요. vgen prepare <엑셀> --out <실행 폴더>: {folder}")
    _require_new_run(folder)
    try:
        with run_lock(folder):
            _require_new_run(folder)
            plan, plan_hash = read_plan(folder)
            jobs = plan["jobs"]
            verify_inputs(jobs)
            if report:
                report(f"입력 검사 완료 · {len(jobs)}건 · 로컬 모델 적재 중")
            render, metadata = load_renderer(folder, plan, plan_hash, load_backend)
            write_json(folder / "run.json", {
                **metadata, "schema_version": 1, "plan_sha256": plan_hash,
                "model_dir": plan["model_dir"], "generation": plan["generation"],
            })
            with (folder / "manifest.jsonl").open("xb") as manifest:
                return run_jobs(folder, jobs, plan["generation"], render, manifest, report)
    except OSError as exc:
        raise GenerateError(f"실행 폴더를 읽거나 기록할 수 없습니다. {folder} ({exc})") from exc
