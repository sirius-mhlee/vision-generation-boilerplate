import json
import os
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from tests.fixtures import WorkbookCase, snapshot
from vgen.generate import GenerateError, generate
from vgen.prepare import prepare
from vgen.resume import resume


def render(sources, *, prompt, seed, generation, output):
    # The same input order, prompt and seed produce exactly the same PNG on a retry.
    key = sha256(json.dumps([prompt, seed]).encode())
    for source in sources:
        key.update(sha256(source).digest())
    Image.new("RGB", (8, 4), tuple(key.digest()[:3])).save(
        output, format="PNG", compress_level=generation["png_compress_level"])
    return {}


class RecoveryTests(WorkbookCase):
    def setUp(self):
        super().setUp()
        self.write_fixture([1, "red", self.guide, None, None, "전선 색"],
                           [2, "evening_cable", self.guide, self.room, self.cable, "배경과 전선 교체"])
        self.folder_run = self.folder / "run"
        self.plan = prepare(self.xlsx, self.folder_run, repeat=2, model_dir=self.folder / "model")
        self.job = self.plan["jobs"][-1]  # Only the second repeat needs repair.
        self.target = self.folder_run / self.job["output_path"]
        self.journal = self.folder_run / "manifest.jsonl"
        self.renderer = Mock(side_effect=render)
        self.loader = Mock(return_value=(self.renderer, {"backend": "test", "model_files": []}))

    @contextmanager
    def stop_at_publish(self):
        replace = os.replace

        def interrupt(source, target):
            replace(source, target)
            if Path(target) == self.target:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt), patch("vgen.generate.os.replace", side_effect=interrupt):
            yield

    def other_files(self):
        paths = [self.folder_run / job["output_path"] for job in self.plan["jobs"][:-1]]
        paths.extend([self.folder_run / "plan.json", self.folder_run / "run.json"])
        return {path: snapshot(path) for path in paths}

    def assert_repairs_one(self):
        before = self.other_files()
        prefix = self.journal.read_bytes()
        self.loader.reset_mock()
        self.renderer.reset_mock()
        result = resume(self.folder_run, load_backend=self.loader)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (3, 1, 0))
        self.loader.assert_called_once()
        self.renderer.assert_called_once()
        args, kwargs = self.renderer.call_args
        self.assertEqual(args[0], [(self.folder / p).read_bytes() for p in (self.guide, self.room, self.cable)])
        self.assertEqual((kwargs["prompt"], kwargs["seed"], kwargs["generation"]),
                         (self.job["model_prompt"], self.job["seed"], self.plan["generation"]))
        self.assertEqual(self.other_files(), before)
        self.assertTrue(self.journal.read_bytes().startswith(prefix))
        last = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual((last["status"], last["output_path"], last["output_sha256"]),
                         ("success", self.job["output_path"], snapshot(self.target)[0]))
        self.assertEqual(last["inputs"], self.job["inputs"])
        self.assertEqual(len(list(self.folder_run.glob("images/*/*.png"))), 4)

        files = {p: snapshot(p) for p in self.folder_run.rglob("*") if p.is_file()}
        self.loader.reset_mock()
        done = resume(self.folder_run, load_backend=self.loader)
        self.loader.assert_not_called()
        self.assertEqual((done.skipped, done.succeeded, done.failed, done.image_dir),
                         (4, 0, 0, self.folder_run / "images"))
        self.assertEqual({p: snapshot(p) for p in files}, files)

    def test_unrecorded_png_from_initial_generation_is_rerendered(self):
        with self.stop_at_publish():
            generate(self.folder_run, load_backend=self.loader)
        self.assertEqual(len(self.journal.read_bytes().splitlines()), 3)
        orphan = self.target.read_bytes()
        self.assert_repairs_one()
        self.assertEqual(self.target.read_bytes(), orphan)

    def test_interrupted_repair_cannot_reuse_the_old_matching_success(self):
        generate(self.folder_run, load_backend=self.loader)
        expected = self.target.read_bytes()
        kept = self.other_files()
        self.target.write_bytes(b"broken PNG")
        with self.stop_at_publish():
            resume(self.folder_run, load_backend=self.loader)
        self.assertEqual(self.target.read_bytes(), expected)
        last = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual((last["status"], last["output_path"]), ("repairing", self.job["output_path"]))
        self.assertEqual(self.other_files(), kept)
        self.assert_repairs_one()

    def test_cannot_replace_existing_file_if_repair_record_cannot_be_written(self):
        generate(self.folder_run, load_backend=self.loader)
        self.target.write_bytes(b"broken PNG")
        self.renderer.reset_mock()
        before = {p: snapshot(p) for p in self.folder_run.rglob("*") if p.is_file()}
        with patch("vgen.generate._append", side_effect=OSError("journal unavailable")) as append:
            with self.assertRaisesRegex(GenerateError, "journal unavailable"):
                resume(self.folder_run, load_backend=self.loader)
        append.assert_called_once()
        self.assertEqual(append.call_args.args[1]["status"], "repairing")
        self.renderer.assert_not_called()
        self.assertEqual({p: snapshot(p) for p in before}, before)
        self.assert_repairs_one()

    def test_invalid_render_keeps_the_previous_file_until_a_successful_repair(self):
        generate(self.folder_run, load_backend=self.loader)
        self.target.write_bytes(b"broken PNG")
        before, peers = snapshot(self.target), self.other_files()

        def broken_render(sources, *, output, **kwargs):
            output.write_bytes(b"partial replacement")
            return {}

        self.renderer.side_effect = broken_render
        result = resume(self.folder_run, load_backend=self.loader)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (3, 0, 1))
        self.assertEqual((snapshot(self.target), self.other_files()), (before, peers))
        self.assertEqual(len(list(self.folder_run.glob("images/*/*.png"))), 4)
        rows = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        self.assertEqual([row["status"] for row in rows[-2:]], ["repairing", "failed"])
        self.renderer.side_effect = render
        self.assert_repairs_one()

    def test_missing_truncated_and_valid_substitute_png_only_repair_one_repeat(self):
        generate(self.folder_run, load_backend=self.loader)
        expected = self.target.read_bytes()
        substitute = (self.folder_run / self.plan["jobs"][1]["output_path"]).read_bytes()
        self.assertNotEqual(substitute, expected)
        for label, data in (("missing", None), ("truncated", expected[:40]), ("substitute", substitute)):
            with self.subTest(case=label):
                if data is None:
                    self.target.unlink()
                else:
                    self.target.write_bytes(data)
                self.assert_repairs_one()
                self.assertEqual(self.target.read_bytes(), expected)

    def test_changed_guide_or_either_reference_blocks_repair_until_restored(self):
        generate(self.folder_run, load_backend=self.loader)
        self.target.unlink()
        before, journal = self.other_files(), snapshot(self.journal)
        for name in (self.guide, self.room, self.cable):
            path = self.folder / name
            with self.subTest(input=name):
                saved = path.read_bytes()
                path.write_bytes(b"changed")
                self.loader.reset_mock()
                with self.assertRaises(GenerateError) as error:
                    resume(self.folder_run, load_backend=self.loader)
                self.assertIn(str(path), str(error.exception))
                self.loader.assert_not_called()
                self.assertEqual((self.other_files(), snapshot(self.journal)), (before, journal))
                self.assertFalse(self.target.exists())
                path.write_bytes(saved)
        self.assert_repairs_one()
