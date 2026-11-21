"""Console entry point for vgen."""

import argparse
import os
import sys
from functools import partial
from pathlib import Path

from .generate import GenerateError, generate
from .prepare import (
    DEFAULT_MAX_EDGE, DEFAULT_MODEL_DIR, DEFAULT_SEED, DEFAULT_STEPS, PrepareError, prepare,
)
from .resume import resume
from .run_files import started
from .workbook import SHEET, WorkbookError, read_workbook


def _print_workbook(filename: Path) -> None:
    edits = read_workbook(filename)
    base = filename.absolute().parent
    print(f"{SHEET} 시트 · 편집 요청 {len(edits)}건")
    for edit in edits:
        inputs = [f"가이드={os.path.relpath(edit.guide, base)}"]
        for ref in edit.references:
            inputs.append(f"레퍼런스 {ref.slot}={os.path.relpath(ref.path, base)}")
        label = f"{edit.display_number}번 {edit.edit_id} (엑셀 {edit.row_number}행)"
        print(f"{label} | {' | '.join(inputs)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vgen", description="엑셀 편집 요청을 계획하고 로컬 GPU에서 PNG 생성")
    commands = parser.add_subparsers(dest="command", required=True)
    workbook = commands.add_parser("workbook", help="편집 시트와 입력 PNG 확인")
    workbook.add_argument("workbook", type=Path, help="편집 요청을 작성한 .xlsx 파일")
    planner = commands.add_parser("prepare", help="입력을 확인하고 실행 폴더에 plan.json 저장")
    planner.add_argument("workbook", type=Path, help="편집 요청을 작성한 .xlsx 파일")
    planner.add_argument("--out", type=Path, required=True, help="계획을 저장할 실행 폴더")
    planner.add_argument("--repeat", type=int, default=1, help="편집별 반복 수 (기본값 1)")
    planner.add_argument("--seed", type=int, default=DEFAULT_SEED, help="기준 시드 (기본값 %(default)s)")
    planner.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR,
                         help="생성 시 사용할 로컬 모델 폴더 (기본값 %(default)s)")
    planner.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                         help="편집당 노이즈 제거 스텝 (기본값 %(default)s)")
    planner.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE,
                         help="패딩 전 입력 사진의 긴 변 상한, 16~1024 px (기본값 %(default)s)")
    generator = commands.add_parser("generate", help="저장된 계획으로 로컬 GPU에서 PNG 생성")
    generator.add_argument("run_dir", type=Path, help="plan.json이 있는 실행 폴더")
    resumer = commands.add_parser("resume", help="기존 PNG를 확인하고 남은 작업 이어가기")
    resumer.add_argument("run_dir", type=Path, help="생성을 시작한 실행 폴더")
    args = parser.parse_args(argv)
    report = partial(print, flush=True)
    try:
        if args.command == "workbook":
            _print_workbook(args.workbook)
            return 0
        if args.command == "generate":
            result = generate(args.run_dir, report=report)
            print(f"성공 {result.succeeded}장 · 실패 {result.failed}건 · 저장 위치 {result.image_dir}")
            return int(result.failed > 0)
        if args.command == "resume":
            result = resume(args.run_dir, report=report)
            print(f"완료 PNG {result.skipped + result.succeeded}장 · 기존 {result.skipped}장 유지 · "
                  f"이번 생성 {result.succeeded}장 · 실패 {result.failed}건 · 저장 위치 {result.image_dir}")
            return int(result.failed > 0)
        plan = prepare(args.workbook, args.out, repeat=args.repeat, seed=args.seed,
                       model_dir=args.model_dir, steps=args.steps, max_edge=args.max_edge)
    except (WorkbookError, PrepareError, GenerateError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command in ("generate", "resume"):
            command = "resume" if started(args.run_dir) else "generate"
            print(f'\n실행을 중단했습니다. 계속하려면 vgen {command} "{args.run_dir}"을 실행하세요.', file=sys.stderr)
            return 130
        raise
    print(f"편집 {plan['edit_count']}행 × 반복 {plan['repeat']}회 = 예상 PNG {plan['job_count']}장")
    jobs = plan["jobs"]
    folders = f"{args.out / Path(jobs[0]['output_path']).parent}/"
    if plan["repeat"] > 1:
        folders += f" … {args.out / Path(jobs[-1]['output_path']).parent}/"
    print(f"저장 위치     {folders}")
    print(f"계획 파일     {args.out / 'plan.json'}")
    return 0
