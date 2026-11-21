import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.fixtures import WorkbookCase
from vgen.cli import main
from vgen.prepare import PrepareError, prepare


class PrepareTests(WorkbookCase):
    def setUp(self):
        super().setUp()
        self.out = self.folder / "run"
        self.target = self.out / "plan.json"
        self.rows = [
            [3, "색", self.guide, None, None, "빨갛게"],
            [1, "전선", self.guide, None, self.cable, "  레퍼런스 2를 추가해 주세요.\n  "],
            [5, "배경과_전선", self.room, self.room, self.cable, "둘 다 적용해 주세요."],
        ]
        self.write_fixture(*self.rows)

    def test_three_rows_twice_freeze_inputs_prompts_and_options(self):
        absent_model = self.folder / "absent-model"
        plan = prepare(self.xlsx, self.out, repeat=2, seed=27, steps=2, max_edge=512,
                       model_dir=absent_model)
        self.assertEqual(json.loads(self.target.read_text()), plan)
        self.assertEqual(list(self.out.iterdir()), [self.target])
        self.assertFalse(absent_model.exists())
        self.assertEqual(plan["model_dir"], str(absent_model))
        self.assertEqual((plan["edit_count"], plan["repeat"], plan["job_count"]), (3, 2, 6))
        self.assertEqual(plan["base_seed"], 27)
        self.assertEqual(plan["generation"], {
            "num_inference_steps": 2, "guidance_scale": 1.0, "max_edge": 512, "png_compress_level": 0,
        })
        jobs = plan["jobs"]
        self.assertEqual(len(jobs), 6)
        self.assertEqual(len({job["seed"] for job in jobs}), 6)
        self.assertEqual([job["repeat_index"] for job in jobs], [1, 1, 1, 2, 2, 2])
        self.assertEqual([job["output_path"] for job in jobs], [
            f"images/r{index:03d}/{row[1]}.png" for index in (1, 2) for row in self.rows
        ])
        for i, job in enumerate(jobs):
            row = self.rows[i % 3]
            self.assertEqual((job["display_number"], job["sheet_row"]), (row[0], 2 + i % 3))
            self.assertEqual(job["user_prompt"], row[-1])
            self.assertTrue(job["model_prompt"].startswith("첫 번째 사진은 편집할 가이드"))
            self.assertTrue(job["model_prompt"].endswith("\n\n" + row[-1]))
            inputs = job["inputs"]
            self.assertEqual([item["path"] for item in inputs],
                             [str(self.folder / path) for path in row[2:5] if path])
            self.assertEqual([item["position"] for item in inputs], list(range(1, len(inputs) + 1)))
            self.assertEqual(inputs[0]["role"], "guide")
            for item in inputs:
                self.assertEqual(item["sha256"], sha256(Path(item["path"]).read_bytes()).hexdigest())
        self.assertEqual(jobs[1]["inputs"][1]["reference_slot"], 2)
        self.assertIn("두 번째 사진은 레퍼런스 2", jobs[1]["model_prompt"])
        self.assertNotIn("레퍼런스 1", jobs[1]["model_prompt"])
        self.assertEqual([i["reference_slot"] for i in jobs[2]["inputs"][1:]], [1, 2])
        self.assertIn("세 번째 사진은 레퍼런스 2", jobs[2]["model_prompt"])

    def test_reordering_and_renumbering_preserve_per_job_identity(self):
        def identity(plan):
            return {
                (job["edit_id"], job["repeat_index"]): (job["seed"], job["output_path"], job["inputs"])
                for job in plan["jobs"]
            }
        original = prepare(self.xlsx, self.out, repeat=2)
        self.write_fixture(*([n, *row[1:]] for n, row in enumerate(reversed(self.rows), start=17)))
        reordered = prepare(self.xlsx, self.out, repeat=2)
        self.assertEqual(identity(original), identity(reordered))
        self.assertEqual(reordered["jobs"][0]["display_number"], 17)
        self.assertEqual(json.loads(self.target.read_text()), reordered)
        different_seed = prepare(self.xlsx, self.out, repeat=2, seed=43)
        self.assertNotEqual([j["seed"] for j in reordered["jobs"]],
                            [j["seed"] for j in different_seed["jobs"]])

    def test_reprepare_errors_leave_old_plan_and_remove_partial_files(self):
        prepare(self.xlsx, self.out)
        before = self.target.read_bytes(), self.target.stat().st_mtime_ns
        self.write_fixture([4, "bad", self.guide, None, "missing.png", "요청"])
        error = StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(["prepare", str(self.xlsx), "--out", str(self.out)]), 1)
        for expected in ("편집!E2", "missing.png", "기존 계획을 유지"):
            self.assertIn(expected, error.getvalue())
        self.write_fixture(*self.rows)
        with patch("vgen.run_files.os.replace", side_effect=OSError("쓰기 실패")):
            with self.assertRaisesRegex(PrepareError, "기존 계획을 유지"):
                prepare(self.xlsx, self.out, seed=111)
        self.assertEqual(list(self.out.iterdir()), [self.target])
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), before)

    def test_started_generation_markers_prevent_replacement(self):
        prepare(self.xlsx, self.out)
        old = self.target.read_bytes()
        for marker in ("run.json", "manifest.jsonl"):
            with self.subTest(marker=marker):
                path = self.out / marker
                path.touch()
                with self.assertRaisesRegex(PrepareError, marker):
                    prepare(self.xlsx, self.out, seed=31)
                self.assertEqual(self.target.read_bytes(), old)
                path.unlink()

    def test_invalid_options_and_missing_workbook_leave_no_new_plan(self):
        for options in ({"repeat": 0}, {"steps": -1}, {"max_edge": 15}, {"max_edge": 1025}):
            with self.subTest(options=options), self.assertRaises(PrepareError):
                prepare(self.xlsx, self.out, **options)
        self.xlsx.unlink()
        with self.assertRaisesRegex(PrepareError, "엑셀 파일이 없습니다"):
            prepare(self.xlsx, self.out)
        self.assertFalse(self.out.exists())

    def test_unusable_model_dimensions_report_the_cell_and_preserve_the_plan(self):
        self.write_fixture([1, "edit", self.guide, None, self.cable, "두 번째 레퍼런스"])
        cable = self.folder / self.cable
        Image.new("RGB", (512, 49)).save(cable)  # Pads to 512 × 64, exactly 8:1.
        prepare(self.xlsx, self.out, max_edge=512)
        original = self.target.read_bytes()
        Image.new("RGB", (513, 64)).save(cable)
        for edge, cell, path, size in ((64, "C2", self.guide, "64×48"),
                                       (1024, "E2", self.cable, "528×64")):
            with self.subTest(edge=edge), redirect_stderr(StringIO()) as errors:
                self.assertEqual(main(["prepare", str(self.xlsx), "--out", str(self.out),
                                       "--max-edge", str(edge)]), 1)
                for part in (f"편집!{cell}", path, size):
                    self.assertIn(part, errors.getvalue())
                self.assertEqual(self.target.read_bytes(), original)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["workbook", str(self.xlsx)]), 0)

    def test_concurrent_prepares_publish_one_complete_plan(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(prepare, self.xlsx, self.out, repeat=n) for n in (1, 2)]
            plans = [future.result() for future in futures]
        self.assertIn(json.loads(self.target.read_text()), plans)
        self.assertEqual(list(self.out.iterdir()), [self.target])

    def test_cli_workbook_and_shipped_five_row_plan(self):
        sample = Path(__file__).resolve().parents[1] / "examples" / "living-room" / "scenarios.xlsx"
        with redirect_stdout(StringIO()) as listing:
            self.assertEqual(main(["workbook", str(sample)]), 0)
        self.assertIn("5번 evening_cable (엑셀 6행)", listing.getvalue())
        with redirect_stdout(StringIO()) as summary:
            self.assertEqual(main(["prepare", str(sample), "--out", str(self.out)]), 0)
        self.assertEqual(summary.getvalue().splitlines(), [
            "편집 5행 × 반복 1회 = 예상 PNG 5장",
            f"저장 위치     {self.out / 'images' / 'r001'}/",
            f"계획 파일     {self.target}",
        ])
        plan = json.loads(self.target.read_text())
        self.assertEqual((plan["job_count"], plan["repeat"], plan["base_seed"]), (5, 1, 42))
        self.assertEqual(plan["generation"]["num_inference_steps"], 4)
        self.assertEqual([job["display_number"] for job in plan["jobs"]], [1, 2, 3, 4, 5])
        self.assertEqual([job["edit_id"] for job in plan["jobs"]],
                         ["red", "night", "add_cable", "evening_bg", "evening_cable"])
        self.assertEqual([[item["reference_slot"] for item in job["inputs"][1:]] for job in plan["jobs"]],
                         [[], [], [1], [1], [1, 2]])
        last = plan["jobs"][-1]
        self.assertEqual(last["output_path"], "images/r001/evening_cable.png")
        self.assertEqual(last["seed"], 6720713048883403)
        self.assertEqual([Path(item["path"]).name for item in last["inputs"]],
                         ["living01.png", "evening_room.png", "black_cable.png"])
