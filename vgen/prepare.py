"""Prepare an inspectable plan for independent image edits on the local workstation."""

import json
from hashlib import sha256
from pathlib import Path

from .run_files import run_lock, started, write_json
from .workbook import EditRow, WorkbookError, read_workbook

DEFAULT_MODEL_DIR = Path("models/FLUX.2-klein-4B")
DEFAULT_SEED = 42
DEFAULT_STEPS = 4
DEFAULT_MAX_EDGE = 1024


class PrepareError(ValueError):
    """A planning error suitable for display in the CLI."""


def _job_seed(base_seed: int, edit_id: str, repeat_index: int) -> int:
    key = json.dumps([base_seed, edit_id, repeat_index], ensure_ascii=False, separators=(",", ":"))
    # The top 53 bits are exactly representable in common JSON readers and in torch.
    digest = sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 11


def _request(edit: EditRow) -> dict:
    inputs = [{
        "position": 1, "role": "guide", "path": str(edit.guide), "sha256": edit.guide_sha256,
    }]
    instructions = ["첫 번째 사진은 편집할 가이드입니다."]
    for ordinal, ref in zip(("두 번째", "세 번째"), edit.references):
        inputs.append({
            "position": len(inputs) + 1, "role": "reference", "reference_slot": ref.slot,
            "path": str(ref.path), "sha256": ref.sha256,
        })
        instructions.append(f"{ordinal} 사진은 레퍼런스 {ref.slot}입니다.")
    return {
        "display_number": edit.display_number,
        "edit_id": edit.edit_id,
        "sheet_row": edit.row_number,
        "inputs": inputs,
        "user_prompt": edit.prompt,
        "model_prompt": " ".join(instructions) + "\n\n" + edit.prompt,
    }


def _check_not_started(folder: Path) -> None:
    if started(folder):
        raise PrepareError(f"{folder}에 생성 기록(run.json 또는 manifest.jsonl)이 있습니다. 새 실행 폴더를 지정하세요.")


def _save_plan(target: Path, plan: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with run_lock(target.parent):
        _check_not_started(target.parent)
        write_json(target, plan)


def prepare(
    workbook: str | Path, out: str | Path, *, repeat: int = 1, seed: int = DEFAULT_SEED,
    model_dir: str | Path = DEFAULT_MODEL_DIR, steps: int = DEFAULT_STEPS,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> dict:
    """Validate the workbook and atomically publish its plan in the run directory."""
    workbook = Path(workbook).absolute()
    target = Path(out) / "plan.json"
    try:
        if any(type(value) is not int for value in (repeat, seed, steps, max_edge)):
            raise PrepareError("--repeat, --seed, --steps, --max-edge에는 정수를 지정하세요.")
        if repeat < 1 or steps < 1:
            raise PrepareError("--repeat와 --steps는 각각 1 이상의 정수로 지정하세요.")
        if not 16 <= max_edge <= 1024:
            raise PrepareError("--max-edge는 16~1024 px 범위로 지정하세요.")
        _check_not_started(target.parent)
        if target.is_file() and workbook.is_file() and target.samefile(workbook):
            raise PrepareError("엑셀 파일과 계획 파일의 경로가 같습니다. 다른 실행 폴더를 지정하세요.")
        requests = [_request(edit) for edit in read_workbook(workbook)]
        jobs = []
        for repeat_index in range(1, repeat + 1):
            for request in requests:
                edit_id = request["edit_id"]
                jobs.append({
                    **request,
                    "repeat_index": repeat_index,
                    "seed": _job_seed(seed, edit_id, repeat_index),
                    "output_path": f"images/r{repeat_index:03d}/{edit_id}.png",
                })
        plan = {
            "schema_version": 1,
            "workbook": str(workbook),
            "model_dir": str(Path(model_dir).absolute()),
            "base_seed": seed,
            "repeat": repeat,
            "edit_count": len(requests),
            "job_count": len(jobs),
            "generation": {
                "num_inference_steps": steps, "guidance_scale": 1.0,
                "max_edge": max_edge, "png_compress_level": 0,
            },
            "jobs": jobs,
        }
        _save_plan(target, plan)
    except (WorkbookError, PrepareError, OSError) as exc:
        message = str(exc)
        if target.is_file():
            message += f"\n기존 계획을 유지했습니다. {target}"
        raise PrepareError(message) from exc
    return plan
