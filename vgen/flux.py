"""Local FLUX inference. GPU dependencies are imported only when inference is requested."""

import os
import platform
from functools import partial
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageOps

from .model_files import check_model_files, file_stamp, fingerprint_model, model_paths

RECORDED_PACKAGES = ("torch", "torchvision", "diffusers", "transformers", "accelerate",
                     "huggingface-hub", "safetensors", "Pillow", "numpy")


def _local_only():
    # Set before importing libraries that cache Hub settings at import time.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def load_pipeline(model_dir: Path, device):
    _local_only()
    import torch
    from diffusers import Flux2KleinPipeline

    gpu = torch.cuda.get_device_properties(device)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    torch.cuda.synchronize(device)
    start = perf_counter()
    pipe = Flux2KleinPipeline.from_pretrained(
        str(model_dir.resolve()), torch_dtype=dtype, local_files_only=True, use_safetensors=True,
    )
    pipe.enable_sequential_cpu_offload(device=device)
    torch.cuda.synchronize(device)
    load_seconds = perf_counter() - start
    return pipe, {
        "model_dir": str(model_dir), "gpu": gpu.name,
        "gpu_total_mib": round(gpu.total_memory / 2**20, 1), "dtype": str(dtype),
        "offload": "sequential CPU offload", "cuda_runtime": torch.version.cuda,
        "python": platform.python_version(),
        "packages": {package: version(package) for package in RECORDED_PACKAGES},
        "model_load_seconds": round(load_seconds, 3),
    }


def prepare_image(source: bytes, max_edge: int):
    import numpy as np

    with Image.open(BytesIO(source), formats=("PNG",)) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
    original_size = rgb.size
    rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    width, height = rgb.size
    # Preserve the scene aspect ratio. Only replicate edge pixels to meet the latent grid.
    padding = ((0, -height % 16), (0, -width % 16), (0, 0))
    pixels = np.pad(np.asarray(rgb), padding, mode="edge")
    return Image.fromarray(pixels), rgb.size, original_size


def render_png(pipe, device, sources: list[bytes], *, prompt, seed, generation, output: Path) -> dict:
    import torch

    prepared = [prepare_image(source, generation["max_edge"]) for source in sources]
    images = [entry[0] for entry in prepared]
    scene, content_size, original_size = prepared[0]
    reference_sizes = {}
    for n, (image, _, size) in enumerate(prepared[1:], start=1):
        prefix = "reference" if n == 1 else f"reference_{n}"
        reference_sizes[f"{prefix}_size"] = size
        reference_sizes[f"{prefix}_model_size"] = image.size
    generator = torch.Generator(device="cpu").manual_seed(seed)
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    start = perf_counter()
    with torch.inference_mode():
        edited = pipe(
            image=images, prompt=prompt, height=scene.height, width=scene.width,
            num_inference_steps=generation["num_inference_steps"],
            guidance_scale=generation["guidance_scale"], generator=generator,
        ).images[0]
    edited = edited.crop((0, 0, *content_size)).resize(original_size, Image.Resampling.LANCZOS)
    edited.save(output, format="PNG", compress_level=generation["png_compress_level"])
    torch.cuda.synchronize(device)
    return {
        "source_size": original_size, "resized_guide_size": content_size,
        "model_size": scene.size, "saved_size": edited.size, **reference_sizes,
        "png_compress_level": generation["png_compress_level"],
        "inference_to_png_seconds": round(perf_counter() - start, 3),
        "peak_cuda_allocated_mib": round(torch.cuda.max_memory_allocated(device) / 2**20, 1),
        "peak_cuda_reserved_mib": round(torch.cuda.max_memory_reserved(device) / 2**20, 1),
    }


def _cuda_device():
    try:
        import torch
    except ImportError as exc:
        raise ValueError("README의 설치 절차에 따라 GPU 추론 패키지를 설치하세요.") from exc
    if not torch.cuda.is_available():
        raise ValueError("PyTorch에서 CUDA GPU를 사용할 수 없습니다.")
    return torch.device("cuda", torch.cuda.current_device())


def open_flux(model_dir: Path, *, expected_files: list[dict] | None = None):
    paths = model_paths(model_dir)
    _local_only()
    # On a fresh run, report a missing GPU before reading gigabytes of weights.
    # On resume, identify a changed model before importing GPU dependencies.
    device = _cuda_device() if expected_files is None else None
    stamps = {path: file_stamp(path) for path in paths}
    records = fingerprint_model(model_dir, paths)
    if expected_files is not None:
        check_model_files(model_dir, expected_files, records)
        device = _cuda_device()
    pipe, metadata = load_pipeline(model_dir, device)
    if model_paths(model_dir) != paths:
        raise ValueError(f"적재 중 로컬 모델 파일 목록이 변경됐습니다. {model_dir}")
    for path, original in stamps.items():
        if file_stamp(path) != original:
            raise ValueError(f"적재 중 로컬 모델 파일이 변경됐습니다. {path}")
    return partial(render_png, pipe, device), {
        **metadata, "backend": "Flux2KleinPipeline", "model_files": records,
    }
