"""Identify the local Diffusers components that define a FLUX.2 klein run."""

import json
from hashlib import file_digest
from pathlib import Path


def _required(path: Path) -> Path:
    if not path.is_file():
        raise ValueError(f"로컬 모델 파일이 없습니다. {path}")
    return path


def _json(path: Path) -> dict:
    try:
        value = json.loads(_required(path).read_bytes())
        if not isinstance(value, dict):
            raise ValueError("JSON 객체가 필요합니다.")
        return value
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _weights(component: Path, stem: str) -> list[Path]:
    single = component / f"{stem}.safetensors"
    index = component / f"{stem}.safetensors.index.json"
    if not index.exists() and single.is_file():
        return [single]
    if not index.is_file():
        raise ValueError(f"로컬 모델 가중치를 찾을 수 없습니다. {single} 또는 {index}")
    mapping = _json(index).get("weight_map")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(f"가중치 조각 목록이 없습니다. {index}")
    for name in mapping.values():
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".safetensors"):
            raise ValueError(f"가중치 조각 경로를 확인하세요. {index}: {name!r}")
    paths = [index, *(_required(component / name) for name in sorted(set(mapping.values())))]
    # Some local exports contain both layouts. Include both candidates used by the loaders.
    if single.is_file():
        paths.append(single)
    return paths


def model_paths(root: Path) -> list[Path]:
    index = root / "model_index.json"
    config = _json(index)
    if config.get("_class_name") != "Flux2KleinPipeline":
        raise ValueError(f"FLUX.2 klein Diffusers 모델 설정을 확인하세요. {index}")
    paths = [index, _required(root / "scheduler/scheduler_config.json")]
    for component, stem in (("text_encoder", "model"), ("transformer", "diffusion_pytorch_model"),
                            ("vae", "diffusion_pytorch_model")):
        paths.append(_required(root / component / "config.json"))
        paths.extend(_weights(root / component, stem))
    optional = root / "text_encoder/generation_config.json"
    if optional.is_file():
        paths.append(optional)

    tokenizer = root / "tokenizer"
    settings = tokenizer / "tokenizer_config.json"
    token_config = _json(settings)
    paths.append(settings)
    if not (tokenizer / "tokenizer.json").is_file():
        _required(tokenizer / "vocab.json")
        _required(tokenizer / "merges.txt")
    templates = sorted((tokenizer / "chat_templates").glob("*.jinja"))
    if not (token_config.get("chat_template") or templates or (tokenizer / "chat_template.json").is_file()):
        _required(tokenizer / "chat_template.jinja")
    paths.extend(templates)
    for name in ("tokenizer.json", "vocab.json", "merges.txt", "added_tokens.json",
                 "special_tokens_map.json", "chat_template.jinja", "chat_template.json"):
        if (tokenizer / name).is_file():
            paths.append(tokenizer / name)
    return sorted(paths)


def file_stamp(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def fingerprint_model(root: Path, paths: list[Path]) -> list[dict]:
    records = []
    for path in paths:
        with path.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        records.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    return records


def check_model_files(root: Path, expected: list[dict], actual: list[dict]) -> None:
    """Compare the inventory saved before the first edit with the current snapshot."""
    saved = {item["path"]: item["sha256"] for item in expected}
    current = {item["path"]: item["sha256"] for item in actual}
    for name in sorted(saved.keys() | current.keys()):
        if name not in current:
            raise ValueError(f"첫 실행의 모델 파일이 누락됐습니다. {root / name}")
        if name not in saved:
            raise ValueError(f"첫 실행 후 모델 파일이 추가됐습니다. {root / name}")
        if current[name] != saved[name]:
            raise ValueError(f"첫 실행과 모델 파일의 SHA-256이 다릅니다. {root / name}")
