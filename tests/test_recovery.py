import json
import os
import subprocess
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from PIL import Image

from tests.fixtures import FluxRunCase, snapshot
from vgen.generate import GenerateError, generate
from vgen.prepare import prepare
from vgen.resume import resume


def render(_pipe, _device, sources, *, prompt, seed, generation, output):
    # The same input order, prompt and seed produce exactly the same PNG on a retry.
    key = sha256(json.dumps([prompt, seed]).encode())
    for source in sources:
        key.update(sha256(source).digest())
    Image.new("RGB", (128, 80), tuple(key.digest()[:3])).save(
        output, format="PNG", compress_level=generation["png_compress_level"])
    return {}


class RecoveryTests(FluxRunCase):
    def setUp(self):
        super().setUp()
        self.write_fixture([1, "red", self.guide, None, None, "전선 색"],
                           [2, "evening_cable", self.guide, self.room, self.cable, "배경과 전선 교체"])
        self.plan = prepare(self.xlsx, self.out, repeat=2, model_dir=self.model)
        self.job = self.plan["jobs"][-1]  # Only the second repeat needs repair.
        self.target = self.out / self.job["output_path"]
        self.journal = self.out / "manifest.jsonl"
        self.renderer.side_effect = render

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
        paths = [self.out / job["output_path"] for job in self.plan["jobs"][:-1]]
        paths.extend([self.out / "plan.json", self.out / "run.json"])
        return {path: snapshot(path) for path in paths}

    def assert_repairs_one(self):
        before = self.other_files()
        prefix = self.journal.read_bytes()
        self.loader.reset_mock()
        self.renderer.reset_mock()
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (3, 1, 0))
        self.loader.assert_called_once()
        self.renderer.assert_called_once()
        args, kwargs = self.renderer.call_args
        self.assertEqual(args[2], [(self.folder / p).read_bytes() for p in (self.guide, self.room, self.cable)])
        self.assertEqual((kwargs["prompt"], kwargs["seed"], kwargs["generation"]),
                         (self.job["model_prompt"], self.job["seed"], self.plan["generation"]))
        self.assertEqual(self.other_files(), before)
        self.assertTrue(self.journal.read_bytes().startswith(prefix))
        last = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual((last["status"], last["output_path"], last["output_sha256"]),
                         ("success", self.job["output_path"], snapshot(self.target)[0]))
        self.assertEqual(last["inputs"], self.job["inputs"])
        self.assertEqual(len(list(self.out.glob("images/*/*.png"))), 4)

        files = {p: snapshot(p) for p in self.out.rglob("*") if p.is_file()}
        self.loader.reset_mock()
        done = resume(self.out)
        self.loader.assert_not_called()
        self.assertEqual((done.skipped, done.succeeded, done.failed, done.image_dir),
                         (4, 0, 0, self.out / "images"))
        self.assertEqual({p: snapshot(p) for p in files}, files)

    def test_hard_stop_during_png_write_leaves_only_completed_images_and_retry_reclaims_staging(self):
        generate(self.out)
        self.target.unlink()
        peers = set(self.out.glob("images/*/*.png"))
        other = self.target.with_name(".vgen-other.tmp")
        other.write_bytes(b"unrelated file")
        original = snapshot(other)
        writer = dedent("""\
            import os
            import sys
            from pathlib import Path
            from vgen.run_files import temporary_output

            with temporary_output(Path(sys.argv[1])) as stage:
                stage.write_bytes(b"partial image")
                os._exit(17)
        """)
        result = subprocess.run([sys.executable, "-c", writer, str(self.target)], timeout=10)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(set(self.out.glob("images/*/*.png")), peers)
        self.assert_repairs_one()
        self.assertEqual(set(self.out.glob("images/*/*")), peers | {self.target, other})
        self.assertEqual(snapshot(other), original)

    def test_unrecorded_png_from_initial_generation_is_rerendered(self):
        with self.stop_at_publish():
            generate(self.out)
        self.assertTrue((self.out / "run.json").is_file())
        recorded = [json.loads(line)["output_path"] for line in self.journal.read_bytes().splitlines()]
        self.assertEqual(recorded, [job["output_path"] for job in self.plan["jobs"][:-1]])
        with self.assertRaisesRegex(GenerateError, "resume"):
            generate(self.out)
        self.loader.assert_called_once()
        orphan = self.target.read_bytes()
        self.assert_repairs_one()
        self.assertEqual(self.target.read_bytes(), orphan)

    def test_interrupted_repair_cannot_reuse_the_old_matching_success(self):
        generate(self.out)
        expected = self.target.read_bytes()
        kept = self.other_files()
        self.target.write_bytes(b"broken PNG")
        with self.stop_at_publish():
            resume(self.out)
        self.assertEqual(self.target.read_bytes(), expected)
        last = json.loads(self.journal.read_bytes().splitlines()[-1])
        self.assertEqual((last["status"], last["output_path"]), ("repairing", self.job["output_path"]))
        self.assertEqual(self.other_files(), kept)
        self.assert_repairs_one()

    def test_cannot_replace_existing_file_if_repair_record_cannot_be_written(self):
        generate(self.out)
        self.target.write_bytes(b"broken PNG")
        self.renderer.reset_mock()
        before = {p: snapshot(p) for p in self.out.rglob("*") if p.is_file()}
        with patch("vgen.generate._append", side_effect=OSError("journal unavailable")) as append:
            with self.assertRaisesRegex(GenerateError, "journal unavailable"):
                resume(self.out)
        append.assert_called_once()
        self.assertEqual(append.call_args.args[1]["status"], "repairing")
        self.renderer.assert_not_called()
        self.assertEqual({p: snapshot(p) for p in before}, before)
        self.assert_repairs_one()

    def test_invalid_render_keeps_the_previous_file_until_a_successful_repair(self):
        generate(self.out)
        self.target.write_bytes(b"broken PNG")
        before, peers = snapshot(self.target), self.other_files()

        def broken_render(_pipe, _device, sources, *, output, **kwargs):
            output.write_bytes(b"partial replacement")
            return {}

        self.renderer.side_effect = broken_render
        result = resume(self.out)
        self.assertEqual((result.skipped, result.succeeded, result.failed), (3, 0, 1))
        self.assertEqual((snapshot(self.target), self.other_files()), (before, peers))
        self.assertEqual(len(list(self.out.glob("images/*/*.png"))), 4)
        rows = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        self.assertEqual([row["status"] for row in rows[-2:]], ["repairing", "failed"])
        self.renderer.side_effect = render
        self.assert_repairs_one()

    def test_missing_truncated_and_valid_substitute_png_only_repair_one_repeat(self):
        generate(self.out)
        expected = self.target.read_bytes()
        substitute = (self.out / self.plan["jobs"][1]["output_path"]).read_bytes()
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
        generate(self.out)
        self.target.unlink()
        before, journal = self.other_files(), snapshot(self.journal)
        for name in (self.guide, self.room, self.cable):
            path = self.folder / name
            with self.subTest(input=name):
                saved = path.read_bytes()
                path.write_bytes(b"changed")
                self.loader.reset_mock()
                with self.assertRaises(GenerateError) as error:
                    resume(self.out)
                self.assertIn(str(path), str(error.exception))
                self.loader.assert_not_called()
                self.assertEqual((self.other_files(), snapshot(self.journal)), (before, journal))
                self.assertFalse(self.target.exists())
                path.write_bytes(saved)
        self.assert_repairs_one()
