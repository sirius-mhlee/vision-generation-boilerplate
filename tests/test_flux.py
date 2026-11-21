import json
import unittest
from contextlib import nullcontext
from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from tests.fixtures import write_test_model
from vgen.flux import prepare_image, render_png
from vgen.model_files import fingerprint_model, model_paths


class ModelFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.index = "text_encoder/model.safetensors.index.json"
        self.shard = "text_encoder/second.safetensors"
        self.expected = write_test_model(self.root)
        (self.root / "README.md").write_text("model card")

    def test_hashes_declared_shards_and_tokenizer_and_ignores_unrelated_files(self):
        def inventory():
            return {row["path"]: row["sha256"]
                    for row in fingerprint_model(self.root, model_paths(self.root))}

        initial = inventory()
        self.assertEqual(set(initial), self.expected)
        self.assertEqual(initial[self.shard], sha256(b"second").hexdigest())
        (self.root / self.shard).write_bytes(b"changed")
        self.assertEqual(inventory(), {**initial, self.shard: sha256(b"changed").hexdigest()})

    def test_reports_missing_shard_and_invalid_index_with_its_path(self):
        (self.root / self.shard).unlink()
        with self.assertRaisesRegex(ValueError, self.shard):
            model_paths(self.root)
        (self.root / self.index).write_text(json.dumps({"weight_map": {"a": []}}))
        with self.assertRaisesRegex(ValueError, self.index):
            model_paths(self.root)


@unittest.skipUnless(find_spec("numpy"), "image preprocessing requires the gpu extra")
class FluxImageTests(unittest.TestCase):
    @staticmethod
    def png(size, color, orientation=1):
        image = Image.new("RGB", size, color)
        exif = image.getexif()
        exif[274] = orientation
        stream = BytesIO()
        image.save(stream, format="PNG", exif=exif)
        return stream.getvalue()

    def test_exif_orientation_and_edge_padding_preserve_content(self):
        source = self.png((65, 97), (19, 63, 21), orientation=6)
        image, content, original = prepare_image(source, 128)
        self.assertEqual((original, content, image.size), ((97, 65), (97, 65), (112, 80)))
        self.assertEqual(image.getpixel((111, 79)), (19, 63, 21))

    def test_all_inputs_go_into_one_call_and_output_returns_to_guide_size(self):
        sources = [self.png((160, 96), "red"), self.png((80, 64), "green"), self.png((64, 96), "blue")]
        pipe = Mock(side_effect=lambda **kwargs: SimpleNamespace(images=[kwargs["image"][0]]))
        torch = Mock()
        torch.inference_mode.return_value = nullcontext()
        torch.cuda.max_memory_allocated.return_value = 2**20
        torch.cuda.max_memory_reserved.return_value = 2 * 2**20
        settings = {"max_edge": 128, "num_inference_steps": 2, "guidance_scale": 1.0,
                    "png_compress_level": 0}
        with TemporaryDirectory() as folder, patch.dict("sys.modules", {"torch": torch}):
            out = Path(folder) / "edit.png"
            metrics = render_png(pipe, "cuda", sources, prompt="계획의 지시", seed=27,
                                 generation=settings, output=out)
            with Image.open(out) as image:
                self.assertEqual((image.format, image.size), ("PNG", (160, 96)))
        pipe.assert_called_once()
        args = pipe.call_args.kwargs
        self.assertEqual([im.getpixel((0, 0)) for im in args["image"]],
                         [(255, 0, 0), (0, 128, 0), (0, 0, 255)])
        self.assertEqual([im.size for im in args["image"]], [(128, 80), (80, 64), (64, 96)])
        self.assertEqual((args["width"], args["height"], args["prompt"], args["num_inference_steps"],
                          args["guidance_scale"]), (128, 80, "계획의 지시", 2, 1.0))
        torch.Generator.assert_called_once_with(device="cpu")
        torch.Generator.return_value.manual_seed.assert_called_once_with(27)
        self.assertIs(args["generator"], torch.Generator.return_value.manual_seed.return_value)
        self.assertEqual((metrics["saved_size"], metrics["png_compress_level"]), ((160, 96), 0))
