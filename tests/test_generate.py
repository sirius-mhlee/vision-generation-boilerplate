import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO

from PIL import Image

from tests.fixtures import FluxRunCase, copy_guide
from vgen.cli import main
from vgen.generate import GenerateError, generate
from vgen.prepare import PrepareError, prepare


class GenerateTests(FluxRunCase):
    def setUp(self):
        super().setUp()
        self.write_fixture(
            [10, "색", self.guide, None, None, "빨간색"],
            [2, "전선", self.guide, None, self.cable, "  레퍼런스 2의 전선\n"],
            [5, "배경", self.room, self.room, self.cable, "배경과 전선"],
        )
        self.plan = prepare(self.xlsx, self.out, repeat=2, model_dir=self.model)

    def manifest(self):
        return [json.loads(line) for line in (self.out / "manifest.jsonl").read_text().splitlines()]

    def test_valid_long_edit_id_can_be_published(self):
        edit_id = "방" * 81
        self.write_fixture([1, edit_id, self.guide, None, None, "편집"])
        prepare(self.xlsx, self.out, model_dir=self.model)
        result = generate(self.out)
        self.assertEqual((result.succeeded, result.failed), (1, 0))
        output = self.out / "images" / "r001" / f"{edit_id}.png"
        self.assertEqual(self.manifest()[0]["output_sha256"], sha256(output.read_bytes()).hexdigest())

    def test_unusable_filename_preserves_the_previous_plan_and_stops_old_plans_before_loading(self):
        edit_id = "방" * 300
        path = self.out / "plan.json"
        original = path.read_bytes()
        self.write_fixture([1, edit_id, self.guide, None, None, "편집"])
        with self.assertRaisesRegex(PrepareError, "편집!B2"):
            prepare(self.xlsx, self.out, model_dir=self.model)
        self.assertEqual(path.read_bytes(), original)

        job = self.plan["jobs"][0]
        job.update(edit_id=edit_id, output_path=f"images/r001/{edit_id}.png")
        path.write_text(json.dumps(self.plan))
        with self.assertRaises(GenerateError):
            generate(self.out)
        self.loader.assert_not_called()
        self.assertFalse((self.out / "run.json").exists())

    def test_frozen_plan_one_loader_and_verified_sources_for_every_repeat(self):
        self.xlsx.unlink()
        calls = []
        def record(pipe, device, sources, **kwargs):
            # The run identity must be durable before the first inference.
            run = json.loads((self.out / "run.json").read_bytes())
            self.assertEqual(run["plan_sha256"], sha256((self.out / "plan.json").read_bytes()).hexdigest())
            calls.append(([sha256(x).hexdigest() for x in sources], kwargs["prompt"], kwargs["seed"]))
            self.assertEqual(kwargs["generation"], self.plan["generation"])
            return copy_guide(pipe, device, sources, **kwargs)
        self.renderer.side_effect = record
        result = generate(self.out)
        self.assertEqual((result.succeeded, result.failed, result.image_dir), (6, 0, self.out / "images"))
        self.loader.assert_called_once_with(self.model, "cuda")
        entries = self.manifest()
        self.assertEqual(len(entries), 6)
        for job, entry, args in zip(self.plan["jobs"], entries, calls, strict=True):
            self.assertEqual(args, ([i["sha256"] for i in job["inputs"]], job["model_prompt"], job["seed"]))
            self.assertEqual(entry["inputs"], job["inputs"])
            self.assertEqual((entry["status"], entry["repeat_index"]), ("success", job["repeat_index"]))
            png = self.out / job["output_path"]
            self.assertEqual(sha256(png.read_bytes()).hexdigest(), entry["output_sha256"])
            with Image.open(png) as image:
                self.assertEqual((image.format, image.size), ("PNG", (128, 80)))
        with self.assertRaisesRegex(GenerateError, "resume"):
            generate(self.out)
        with self.assertRaisesRegex(PrepareError, "생성 기록"):
            prepare(self.xlsx, self.out)
        self.assertEqual(self.loader.call_count, 1)

    def test_job_failure_does_not_publish_partial_png_or_stop_later_jobs(self):
        failed = self.plan["jobs"][1]
        def render(pipe, device, sources, **kwargs):
            if kwargs["seed"] == failed["seed"]:
                kwargs["output"].write_bytes(b"partial")
                raise RuntimeError("테스트 추론 실패")
            return copy_guide(pipe, device, sources, **kwargs)
        self.renderer.side_effect = render
        stdout = StringIO()
        with redirect_stdout(stdout):
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
                        generate(self.out)
                    self.loader.assert_not_called()
                    self.assertFalse((self.out / "run.json").exists())
                    self.assertFalse((self.out / "manifest.jsonl").exists())
            path.write_bytes(original)

    def test_mid_run_input_change_stops_before_affected_model_call(self):
        def render(pipe, device, sources, **kwargs):
            (self.folder / self.cable).write_bytes(b"changed")
            return copy_guide(pipe, device, sources, **kwargs)
        self.renderer.side_effect = render
        with self.assertRaisesRegex(GenerateError, str(self.folder / self.cable)):
            generate(self.out)
        self.assertEqual(self.renderer.call_count, 1)
        self.assertEqual([r["status"] for r in self.manifest()], ["success", "failed"])

    def test_invalid_or_unprepared_run_does_not_load_gpu(self):
        for src in (self.xlsx, self.folder, self.folder / "missing"):
            with self.subTest(src=src), self.assertRaisesRegex(GenerateError, "vgen prepare"):
                generate(src)
        self.plan["jobs"][0]["output_path"] = "../outside.png"
        (self.out / "plan.json").write_text(json.dumps(self.plan))
        error = StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(["generate", str(self.out)]), 1)
        self.assertIn("출력 경로", error.getvalue())
        self.loader.assert_not_called()
        self.assertFalse((self.folder / "outside.png").exists())

    def test_startup_failure_can_be_retried(self):
        self.loader.side_effect = RuntimeError("모델 적재 실패")
        with self.assertRaisesRegex(GenerateError, "모델 적재 실패"):
            generate(self.out)
        self.assertFalse((self.out / "run.json").exists())
        self.loader.side_effect = None
        self.assertEqual(generate(self.out).succeeded, 6)

    def test_only_one_generate_can_start_and_prepare_cannot_race_the_claim(self):
        loading, release = self.pause_model_load()
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(generate, self.out)
            self.assertTrue(loading.wait(5))
            second = pool.submit(generate, self.out)
            replacement = pool.submit(prepare, self.xlsx, self.out, seed=77)
            release.set()
            self.assertEqual(first.result().succeeded, 6)
            with self.assertRaisesRegex(GenerateError, "resume"):
                second.result()
            with self.assertRaises(PrepareError):
                replacement.result()
        self.loader.assert_called_once()
        self.assertEqual(json.loads((self.out / "plan.json").read_bytes()), self.plan)
