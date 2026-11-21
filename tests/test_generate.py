import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from PIL import Image

from tests.fixtures import WorkbookCase
from vgen.cli import main
from vgen.generate import GenerateError, generate
from vgen.prepare import PrepareError, prepare


def copy_png(sources, *, prompt, seed, generation, output):
    with Image.open(BytesIO(sources[0])) as guide:
        guide.save(output, format="PNG", compress_level=generation["png_compress_level"])
    return {"input_count": len(sources)}


class GenerateTests(WorkbookCase):
    def setUp(self):
        super().setUp()
        self.out = self.folder / "run"
        self.write_fixture(
            [10, "색", self.guide, None, None, "빨간색"],
            [2, "전선", self.guide, None, self.cable, "  레퍼런스 2의 전선\n"],
            [5, "배경", self.room, self.room, self.cable, "배경과 전선"],
        )
        self.plan = prepare(self.xlsx, self.out, repeat=2)
        self.loader = Mock(return_value=(copy_png, {"backend": "test-copy", "model_files": []}))

    def manifest(self):
        return [json.loads(line) for line in (self.out / "manifest.jsonl").read_text().splitlines()]

    def test_frozen_plan_one_loader_and_verified_sources_for_every_repeat(self):
        self.write_fixture([99, "다른 편집", self.room, None, None, "다른 지시"])
        self.xlsx.unlink()
        calls = []
        def record(sources, **kwargs):
            # The run identity must be durable before the first inference.
            run = json.loads((self.out / "run.json").read_bytes())
            self.assertEqual(run["plan_sha256"], sha256((self.out / "plan.json").read_bytes()).hexdigest())
            calls.append(([sha256(x).hexdigest() for x in sources], kwargs["prompt"], kwargs["seed"]))
            self.assertEqual(kwargs["generation"], self.plan["generation"])
            return copy_png(sources, **kwargs)
        self.loader.return_value = record, {"backend": "test-copy", "model_files": []}
        result = generate(self.out, load_backend=self.loader)
        self.assertEqual((result.succeeded, result.failed, result.image_dir), (6, 0, self.out / "images"))
        self.loader.assert_called_once_with(Path(self.plan["model_dir"]))
        entries = self.manifest()
        self.assertEqual(len(entries), 6)
        for job, entry, args in zip(self.plan["jobs"], entries, calls, strict=True):
            self.assertEqual(args, ([i["sha256"] for i in job["inputs"]], job["model_prompt"], job["seed"]))
            self.assertEqual(entry["inputs"], job["inputs"])
            self.assertEqual((entry["status"], entry["repeat_index"]), ("success", job["repeat_index"]))
            png = self.out / job["output_path"]
            self.assertEqual(sha256(png.read_bytes()).hexdigest(), entry["output_sha256"])
            with Image.open(png) as image:
                self.assertEqual((image.format, image.size), ("PNG", (8, 4)))
        with self.assertRaisesRegex(GenerateError, "resume"):
            generate(self.out, load_backend=self.loader)
        with self.assertRaisesRegex(PrepareError, "생성 기록"):
            prepare(self.xlsx, self.out)
        self.assertEqual(self.loader.call_count, 1)

    def test_job_failure_does_not_publish_partial_png_or_stop_later_jobs(self):
        failed = self.plan["jobs"][1]
        def render(sources, **kwargs):
            if kwargs["seed"] == failed["seed"]:
                kwargs["output"].write_bytes(b"partial")
                raise RuntimeError("테스트 추론 실패")
            return copy_png(sources, **kwargs)
        self.loader.return_value = render, {"backend": "test"}
        stdout = StringIO()
        with patch("vgen.flux.open_flux", self.loader), redirect_stdout(stdout):
            self.assertEqual(main(["generate", str(self.out)]), 1)
        self.assertIn("성공 5장 · 실패 1건", stdout.getvalue())
        entries = self.manifest()
        self.assertEqual([r["status"] for r in entries], ["success", "failed"] + ["success"] * 4)
        self.assertIsNone(entries[1]["output_sha256"])
        self.assertIn("테스트 추론 실패", entries[1]["error"])
        self.assertFalse((self.out / failed["output_path"]).exists())
        self.assertEqual(len(list((self.out / "images").rglob("*.png"))), 5)

    def test_changed_or_missing_guide_or_reference_prevents_model_load(self):
        for name in (self.guide, self.cable):
            path = self.folder / name
            original = path.read_bytes()
            for missing in (False, True):
                with self.subTest(name=name, missing=missing):
                    if missing:
                        path.unlink()
                    else:
                        path.write_bytes((self.folder / self.room).read_bytes())
                    with self.assertRaisesRegex(GenerateError, str(path)):
                        generate(self.out, load_backend=self.loader)
                    self.loader.assert_not_called()
                    self.assertFalse((self.out / "run.json").exists())
                    self.assertFalse((self.out / "manifest.jsonl").exists())
            path.write_bytes(original)

    def test_mid_run_input_change_stops_before_affected_model_call(self):
        def render(sources, **kwargs):
            (self.folder / self.cable).write_bytes(b"changed")
            return copy_png(sources, **kwargs)
        self.loader.return_value = Mock(side_effect=render), {}
        with self.assertRaisesRegex(GenerateError, str(self.folder / self.cable)):
            generate(self.out, load_backend=self.loader)
        self.assertEqual(self.loader.return_value[0].call_count, 1)
        self.assertEqual([r["status"] for r in self.manifest()], ["success", "failed"])

    def test_invalid_or_unprepared_run_does_not_load_gpu(self):
        for src in (self.xlsx, self.folder, self.folder / "missing"):
            with self.subTest(src=src), self.assertRaisesRegex(GenerateError, "vgen prepare"):
                generate(src, load_backend=self.loader)
        self.plan["jobs"][0]["output_path"] = "../outside.png"
        (self.out / "plan.json").write_text(json.dumps(self.plan))
        error = StringIO()
        with patch("vgen.flux.open_flux", self.loader), redirect_stderr(error):
            self.assertEqual(main(["generate", str(self.out)]), 1)
        self.assertIn("출력 경로", error.getvalue())
        self.loader.assert_not_called()
        self.assertFalse((self.folder / "outside.png").exists())

    def test_interrupt_after_png_publish_never_reports_success(self):
        def interrupt(_stream, entry):
            png = self.out / entry["output_path"]
            self.assertEqual(sha256(png.read_bytes()).hexdigest(), entry["output_sha256"])
            raise KeyboardInterrupt

        with patch("vgen.generate._append", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            generate(self.out, load_backend=self.loader)
        self.assertTrue((self.out / "run.json").is_file())
        self.assertEqual(self.manifest(), [])
        first_output = self.out / self.plan["jobs"][0]["output_path"]
        self.assertEqual(list((self.out / "images").rglob("*.png")), [first_output])
        with self.assertRaises(GenerateError):
            generate(self.out, load_backend=self.loader)
        self.loader.assert_called_once()

    def test_startup_failure_can_be_retried(self):
        self.loader.side_effect = RuntimeError("모델 적재 실패")
        with self.assertRaisesRegex(GenerateError, "모델 적재 실패"):
            generate(self.out, load_backend=self.loader)
        self.assertFalse((self.out / "run.json").exists())
        self.loader.side_effect = None
        self.assertEqual(generate(self.out, load_backend=self.loader).succeeded, 6)

    def test_only_one_generate_can_start_and_prepare_cannot_race_the_claim(self):
        loading, release = Event(), Event()
        def wait_loader(_):
            loading.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return copy_png, {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(generate, self.out, load_backend=wait_loader)
            self.assertTrue(loading.wait(5))
            second = pool.submit(generate, self.out, load_backend=self.loader)
            replacement = pool.submit(prepare, self.xlsx, self.out, seed=77)
            release.set()
            self.assertEqual(first.result().succeeded, 6)
            with self.assertRaisesRegex(GenerateError, "resume"):
                second.result()
            with self.assertRaises(PrepareError):
                replacement.result()
        self.loader.assert_not_called()
        self.assertEqual(json.loads((self.out / "plan.json").read_bytes()), self.plan)
