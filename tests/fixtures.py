import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from vgen.workbook import HEADERS


def snapshot(path: Path) -> tuple[str, int]:
    return sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def write_test_model(root: Path) -> set[str]:
    files = {
        "model_index.json": {"_class_name": "Flux2KleinPipeline"},
        "scheduler/scheduler_config.json": {},
        "text_encoder/config.json": {},
        "text_encoder/model.safetensors.index.json": {
            "weight_map": {"a": "first.safetensors", "b": "second.safetensors", "c": "first.safetensors"},
        },
        "text_encoder/first.safetensors": b"first",
        "text_encoder/second.safetensors": b"second",
        "transformer/config.json": {},
        "transformer/diffusion_pytorch_model.safetensors": b"transformer",
        "vae/config.json": {},
        "vae/diffusion_pytorch_model.safetensors": b"vae",
        "tokenizer/tokenizer_config.json": {},
        "tokenizer/tokenizer.json": {},
        "tokenizer/chat_template.jinja": b"template",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return set(files)


class WorkbookCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.xlsx = self.folder / "scenarios.xlsx"
        photos = self.folder / "사진"
        photos.mkdir()
        for name, color in (("guide", "gray"), ("room", "orange"), ("cable", "black")):
            Image.new("RGB", (128, 80), color).save(photos / f"{name}.png", compress_level=0)
        self.guide = "사진/guide.png"
        self.room = "사진/room.png"
        self.cable = "사진/cable.png"

    def write_fixture(self, *rows, headers=HEADERS, title="편집"):
        book = Workbook(write_only=True)
        sheet = book.create_sheet(title)
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        book.save(self.xlsx)
        book.close()


def copy_guide(_pipe, _device, sources, *, output, **kwargs):
    output.write_bytes(sources[0])
    return {}


class FluxRunCase(WorkbookCase):
    """Exercise the real model inventory and run lifecycle without GPU inference."""

    def setUp(self):
        super().setUp()
        self.out = self.folder / "run"
        self.model = self.folder / "model"
        write_test_model(self.model)
        self.enterContext(patch("vgen.flux._cuda_device", return_value="cuda"))
        self.loader = self.enterContext(patch("vgen.flux.load_pipeline", return_value=(None, {})))
        self.renderer = self.enterContext(patch("vgen.flux.render_png", side_effect=copy_guide))

    def pause_model_load(self):
        loading, release = Event(), Event()

        def wait_loader(_model, _device):
            loading.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return None, {}

        self.loader.side_effect = wait_loader
        return loading, release
