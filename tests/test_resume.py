import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import FluxRunCase, copy_guide, snapshot
from vgen.cli import main
from vgen.generate import GenerateError, generate
from vgen.prepare import prepare
from vgen.resume import resume


class ResumeTests(FluxRunCase):
    def setUp(self):
        super().setUp()
        self.write_fixture(
            [1, "red", self.guide, None, None, "색 변경"],
            [2, "shape", self.room, None, None, "형태 변경"],
            [3, "both", self.room, self.room, self.cable, "배경과 케이블"],
        )
        self.plan = prepare(self.xlsx, self.out, model_dir=self.model)
        self.jobs = self.plan["jobs"]
        self.journal = self.out / "manifest.jsonl"
        self.red = self.out / self.jobs[0]["output_path"]

    def interrupt_shape(self):
        def interrupt(pipe, device, sources, **kwargs):
            if kwargs["seed"] == self.jobs[1]["seed"]:
                raise KeyboardInterrupt
            return copy_guide(pipe, device, sources, **kwargs)
        with patch("vgen.flux.render_png", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            generate(self.out)
        self.loader.reset_mock()

    def test_resume_preserves_red_and_sends_each_remaining_job_its_frozen_inputs(self):
        self.interrupt_shape()
        first = self.journal.read_bytes()
        frozen = {path: snapshot(path) for path in (self.red, self.out / "plan.json", self.out / "run.json")}
        self.xlsx.unlink()
        (self.folder / self.guide).unlink()  # Used only by the finished edit.
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (1, 2, 0))
        self.loader.assert_called_once_with(self.model, "cuda")
        for call, job in zip(self.renderer.call_args_list, self.jobs[1:], strict=True):
            self.assertEqual(call.args[2], [Path(src["path"]).read_bytes() for src in job["inputs"]])
            self.assertEqual((call.kwargs["seed"], call.kwargs["prompt"], call.kwargs["generation"]),
                             (job["seed"], job["model_prompt"], self.plan["generation"]))
        self.assertTrue(self.journal.read_bytes().startswith(first))
        entries = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        self.assertEqual([entry["edit_id"] for entry in entries], ["red", "shape", "both"])
        self.assertEqual({path: snapshot(path) for path in frozen}, frozen)

        (self.folder / self.cable).unlink()
        (self.model / "model_index.json").unlink()
        all_files = {path: snapshot(path) for path in self.out.rglob("*") if path.is_file()}
        console = StringIO()
        with patch("vgen.flux.open_flux") as gpu, redirect_stdout(console):
            self.assertEqual(main(["resume", str(self.out)]), 0)
        gpu.assert_not_called()
        self.assertIn("완료 PNG 3장 · 기존 3장 유지 · 이번 생성 0장", console.getvalue())
        self.assertIn(str(self.out / "images"), console.getvalue())
        self.assertEqual({path: snapshot(path) for path in all_files}, all_files)

    def test_changed_or_missing_model_files_are_named_before_gpu_load(self):
        self.interrupt_shape()
        saved = self.journal.read_bytes()
        edits = (
            ("text_encoder/first.safetensors", b"different weights"),
            ("tokenizer/chat_template.jinja", b"different template"),
            ("scheduler/scheduler_config.json", b'{"different":true}'),
            ("vae/config.json", None),
            ("tokenizer/added_tokens.json", b"{}"),
        )
        for name, data in edits:
            path = self.model / name
            before = path.read_bytes() if path.exists() else None
            with self.subTest(name=name):
                if data is None:
                    path.unlink()
                else:
                    path.write_bytes(data)
                with self.assertRaisesRegex(GenerateError, re.escape(str(path))):
                    resume(self.out)
                self.assertEqual(self.journal.read_bytes(), saved)
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
        self.loader.assert_not_called()
        self.assertEqual(resume(self.out).succeeded, 2)

    def test_pending_input_change_and_changed_plan_stop_before_loading(self):
        self.interrupt_shape()
        frozen = snapshot(self.journal)
        ref = self.folder / self.cable
        image = ref.read_bytes()
        ref.write_bytes(b"changed")
        with self.assertRaisesRegex(GenerateError, re.escape(str(ref))):
            resume(self.out)
        ref.unlink()
        with self.assertRaisesRegex(GenerateError, re.escape(str(ref))):
            resume(self.out)
        ref.write_bytes(image)
        plan = self.out / "plan.json"
        plan.write_bytes(plan.read_bytes() + b" ")
        with self.assertRaisesRegex(GenerateError, re.escape(str(plan))):
            resume(self.out)
        plan.unlink()
        with self.assertRaisesRegex(GenerateError, "복원"):
            resume(self.out)
        self.loader.assert_not_called()
        self.assertEqual(snapshot(self.journal), frozen)

    def test_cli_distinguishes_preparation_interruption_and_missing_run_record(self):
        with self.assertRaisesRegex(GenerateError, "vgen prepare"):
            resume(self.folder)
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(["resume", str(self.out)]), 1)
            self.assertIn("vgen generate", errors.getvalue())
            self.loader.assert_not_called()
            with patch("vgen.flux.render_png", side_effect=KeyboardInterrupt), redirect_stdout(StringIO()):
                self.assertEqual(main(["generate", str(self.out)]), 130)
        self.assertIn("vgen resume", errors.getvalue())
        self.journal.unlink()  # Interrupt between publishing run.json and opening the journal.
        self.assertEqual(resume(self.out).succeeded, 3)
        (self.out / "run.json").unlink()
        with self.assertRaisesRegex(GenerateError, "run.json"):
            resume(self.out)

    def test_retries_only_the_failed_repeat_and_keeps_previous_valid_results(self):
        plan = prepare(self.xlsx, self.out, repeat=2, model_dir=self.model)
        failed_job = plan["jobs"][4]
        def fail_one(pipe, device, sources, **kwargs):
            if kwargs["seed"] == failed_job["seed"]:
                raise RuntimeError("test failure")
            return copy_guide(pipe, device, sources, **kwargs)
        with patch("vgen.flux.render_png", side_effect=fail_one):
            self.assertEqual(generate(self.out).failed, 1)
        kept = {path: snapshot(path) for path in self.out.glob("images/*/*.png")}
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded), (5, 1))
        self.assertEqual(self.renderer.call_args.kwargs["seed"], failed_job["seed"])
        self.assertEqual({path: snapshot(path) for path in kept}, kept)
        last = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual((last["edit_id"], last["repeat_index"], last["status"]), ("shape", 2, "success"))

    def test_failed_resume_reports_error_continues_later_edit_and_can_retry(self):
        self.interrupt_shape()
        def fail_shape(pipe, device, sources, **kwargs):
            if kwargs["seed"] == self.jobs[1]["seed"]:
                kwargs["output"].write_bytes(b"partial")
                raise RuntimeError("형태 편집 실패")
            return copy_guide(pipe, device, sources, **kwargs)

        console = StringIO()
        with patch("vgen.flux.render_png", side_effect=fail_shape), redirect_stdout(console):
            self.assertEqual(main(["resume", str(self.out)]), 1)
        self.assertIn("기존 1장 유지 · 이번 생성 1장 · 실패 1건", console.getvalue())
        self.assertFalse((self.out / self.jobs[1]["output_path"]).exists())
        entries = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        self.assertEqual([row["status"] for row in entries], ["success", "failed", "success"])
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (2, 1, 0))

    def test_partial_tail_is_replaced_and_full_record_without_newline_is_preserved(self):
        self.interrupt_shape()
        first = self.journal.read_bytes()
        red_before = snapshot(self.red)
        self.journal.write_bytes(first + b'{"edit_id":"sh')
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded), (1, 2))
        self.assertEqual(snapshot(self.red), red_before)
        self.assertTrue(self.journal.read_bytes().startswith(first))
        self.journal.write_bytes(first.rstrip(b"\n"))
        with self.journal.open("rb") as stream:
            self.assertEqual(json.load(stream)["edit_id"], "red")
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded), (1, 2))
        self.assertEqual([json.loads(row)["edit_id"] for row in self.journal.read_bytes().splitlines()],
                         ["red", "shape", "both"])

    def test_inconsistent_or_unreadable_complete_manifest_line_is_reported(self):
        self.interrupt_shape()
        record = json.loads(self.journal.read_bytes())
        for raw in (b"bad JSON\n", (json.dumps({**record, "seed": 12345}) + "\n").encode()):
            with self.subTest(raw=raw[:10]):
                self.journal.write_bytes(raw)
                with self.assertRaisesRegex(GenerateError, "manifest.jsonl 1행"):
                    resume(self.out)
                self.assertEqual(self.journal.read_bytes(), raw)
        self.loader.assert_not_called()

    def test_concurrent_resumers_recheck_outputs_after_taking_the_lock(self):
        self.interrupt_shape()
        loading, release = self.pause_model_load()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(resume, self.out)
            self.assertTrue(loading.wait(5))
            second = pool.submit(resume, self.out)
            release.set()
            self.assertEqual(first.result().succeeded, 2)
            self.assertEqual(second.result().skipped, 3)
        self.loader.assert_called_once()

    def test_external_journal_change_during_model_loading_is_preserved(self):
        self.interrupt_shape()
        prefix = self.journal.read_bytes()
        updated = prefix + b'{"unfinished":'
        def change_journal(_model, _device):
            self.journal.write_bytes(updated)
            return None, {}

        self.loader.side_effect = change_journal
        with self.assertRaisesRegex(GenerateError, "manifest.jsonl"):
            resume(self.out)
        self.renderer.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), updated)
