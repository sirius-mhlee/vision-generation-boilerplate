"""로컬 FLUX로 거실의 양말을 지우고 전선을 놓은 뒤 책상 다리 뒤로 옮긴다."""

import argparse
import json
import os
import platform
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

# The Hub libraries read this setting at import time.
os.environ["HF_HUB_OFFLINE"] = "1"

import numpy as np
import torch
from diffusers import Flux2KleinPipeline
from PIL import Image, ImageOps


PROMPT_A = "노란 양말만 사진에서 깨끗하게 지워 주세요. 사진의 나머지 부분은 그대로 두세요."
PROMPT_B = (
    "오른쪽 아래의 빈 타일 바닥에 한 뼘짜리 검정 USB-C 케이블 하나를 추가하세요. "
    "양쪽 은색 단자가 모두 보이는 가로로 긴 S자입니다. 바닥 위에 놓인 케이블 전체가 보입니다."
)
REFERENCE_INSTRUCTION = "첫 번째 사진에 두 번째 사진의 케이블을 놓으세요. "
PROMPT_C = (
    "같은 전선의 가운데 부분을 오른쪽 큰 나무 책상 다리 바로 뒤 바닥으로 옮겨 주세요. "
    "다리가 가운데를 가리고, 검은 전선과 플러그가 다리 왼쪽과 오른쪽에서 각각 나오게 해 주세요. "
    "책상 다리의 위치와 거실은 그대로 두세요."
)
STEPS = 4
GUIDANCE = 1.0
RECORDED_PACKAGES = (
    "torch", "torchvision", "diffusers", "transformers", "accelerate",
    "huggingface-hub", "safetensors", "Pillow", "numpy",
)


def prepare_image(path, max_edge):
    source = path.read_bytes()
    with Image.open(BytesIO(source)) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
    original_size = rgb.size
    rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    width, height = rgb.size
    # The VAE and latent packing use a 16-pixel grid. Extend only the edges.
    padding = ((0, -height % 16), (0, -width % 16), (0, 0))
    pixels = np.pad(np.asarray(rgb), padding, mode="edge")
    return Image.fromarray(pixels), rgb.size, original_size, sha256(source).hexdigest()


def parse_args(only_a):
    description = "로컬 FLUX로 가이드에 편집 지시 한 건을 적용한다. 기본 지시는 노란 양말 제거다." if only_a else __doc__
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--guide", type=Path, default=Path("pilot/guides/living01.png"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/FLUX.2-klein-4B"))
    parser.add_argument("--out", type=Path, help="새 결과 폴더")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--prompt", default=PROMPT_A, help="A의 한글 편집 지시")
    parser.add_argument("--max-edge", type=int, default=1024, help="각 모델 입력의 긴 변 (16~1024 px)")
    if not only_a:
        parser.add_argument("--reference", type=Path, help="B에서 두 번째 입력으로 쓸 전선 사진")
        parser.add_argument("--prompt-b", help="B의 전체 지시를 교체")
        parser.add_argument("--prompt-c", default=PROMPT_C, help="C의 편집 지시")
    args = parser.parse_args()
    if not args.out:
        if only_a:
            args.out = Path("runs/pilot-a")
        elif args.reference:
            args.out = Path("runs/pilot-abc-with-ref")
        else:
            args.out = Path("runs/pilot-abc-no-ref")
    if not 16 <= args.max_edge <= 1024:
        parser.error("--max-edge는 16~1024 px 범위로 지정하세요.")
    if args.out.exists():
        parser.error(f"결과 폴더가 이미 있습니다. --out으로 새 경로를 지정하세요: {args.out}")
    if not args.guide.is_file():
        parser.error(f"가이드 사진을 찾을 수 없습니다: {args.guide}")
    if not only_a and args.reference and not args.reference.is_file():
        parser.error(f"전선 참고 사진을 찾을 수 없습니다: {args.reference}")
    if not (args.model_dir / "model_index.json").is_file():
        parser.error(f"Diffusers 모델 폴더를 확인하세요: {args.model_dir}")
    if not torch.cuda.is_available():
        parser.error("PyTorch에서 CUDA GPU를 사용할 수 없습니다.")
    return args


def load_pipeline(model_dir, device):
    gpu = torch.cuda.get_device_properties(device)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    torch.cuda.synchronize(device)
    start = perf_counter()
    pipe = Flux2KleinPipeline.from_pretrained(
        str(model_dir.resolve()), torch_dtype=dtype, local_files_only=True, use_safetensors=True
    )
    pipe.enable_sequential_cpu_offload(device=device)
    torch.cuda.synchronize(device)
    load_seconds = perf_counter() - start
    return pipe, {
        "model_dir": str(model_dir),
        "gpu": gpu.name,
        "gpu_total_mib": round(gpu.total_memory / 2**20, 1),
        "dtype": str(dtype),
        "offload": "sequential CPU offload",
        "cuda_runtime": torch.version.cuda,
        "python": platform.python_version(),
        "packages": {package: version(package) for package in RECORDED_PACKAGES},
        "model_load_seconds": round(load_seconds, 3),
        "num_inference_steps": STEPS,
        "guidance_scale": GUIDANCE,
    }


def edit_to_png(pipe, device, inputs, prompt, seed, max_edge, output):
    scene, content_size, original_size, source_hash = prepare_image(inputs[0], max_edge)
    images = [scene]
    input_hashes = [source_hash]
    reference_metrics = {}
    if len(inputs) == 2:
        reference, _, reference_size, reference_hash = prepare_image(inputs[1], max_edge)
        images.append(reference)
        input_hashes.append(reference_hash)
        reference_metrics = {"reference_size": reference_size, "reference_model_size": reference.size}

    generator = torch.Generator(device="cpu").manual_seed(seed)
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    start = perf_counter()
    with torch.inference_mode():
        edited = pipe(
            image=images, prompt=prompt, height=scene.height, width=scene.width,
            num_inference_steps=STEPS, guidance_scale=GUIDANCE, generator=generator,
        ).images[0]
    edited = edited.crop((0, 0, *content_size)).resize(original_size, Image.Resampling.LANCZOS)
    edited.save(output)
    torch.cuda.synchronize(device)
    elapsed = perf_counter() - start
    return {
        "output": output.name,
        "output_sha256": sha256(output.read_bytes()).hexdigest(),
        "input_sha256": input_hashes,
        "prompt": prompt,
        "seed": seed,
        "source_size": original_size,
        "resized_guide_size": content_size,
        "model_size": scene.size,
        "saved_size": edited.size,
        **reference_metrics,
        "inference_to_png_seconds": round(elapsed, 3),
        "peak_cuda_allocated_mib": round(torch.cuda.max_memory_allocated(device) / 2**20, 1),
        "peak_cuda_reserved_mib": round(torch.cuda.max_memory_reserved(device) / 2**20, 1),
    }


def main(*, only_a=False):
    args = parse_args(only_a)
    device = torch.device("cuda", torch.cuda.current_device())
    pipe, metrics = load_pipeline(args.model_dir, device)
    metrics["guide"] = str(args.guide)
    metrics["base_seed"] = args.seed
    metrics["max_edge"] = args.max_edge

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.out.parent, prefix=".pilot-") as temp:
        result_dir = Path(temp) / "result"
        result_dir.mkdir()
        edits = [("A", [args.guide], args.prompt)]
        if not only_a:
            inputs_b = [result_dir / "A.png"]
            prompt_b = PROMPT_B
            if args.reference:
                inputs_b.append(args.reference)
                prompt_b = REFERENCE_INSTRUCTION + prompt_b
            edits.extend([
                ("B", inputs_b, args.prompt_b or prompt_b),
                ("C", [result_dir / "B.png"], args.prompt_c),
            ])

        records = {}
        for name, inputs, prompt in edits:
            output = result_dir / f"{name}.png"
            record = edit_to_png(pipe, device, inputs, prompt, args.seed, args.max_edge, output)
            record["inputs"] = [p.name if p.parent == result_dir else str(p) for p in inputs]
            records[name] = record
        if only_a:
            metrics.update(records["A"])
        else:
            metrics["edits"] = records
        (result_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result_dir.rename(args.out)
    for record in records.values():
        print(f"{args.out / record['output']}  ({record['inference_to_png_seconds']:.2f} s)")
    print(args.out / "metrics.json")


if __name__ == "__main__":
    main()
