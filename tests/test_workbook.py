from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from PIL import Image

from tests.fixtures import WorkbookCase
from vgen.cli import main
from vgen.workbook import HEADERS, WorkbookError, read_workbook


class WorkbookTests(WorkbookCase):
    def expect_error(self, *fragments):
        with self.assertRaises(WorkbookError) as raised:
            read_workbook(self.xlsx)
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))

    def test_reference_slots_row_numbers_and_read_only_input(self):
        prompt = "  레퍼런스 2의 전선으로 교체해 주세요.\n낮 조명을 유지해 주세요.  "
        self.write_fixture(
            [30, "001", self.guide, None, "  ", "낮"],
            [],
            [10, "왼쪽", self.guide, self.room, None, "저녁"],
            [" ", " ", " ", " ", " ", " "],
            [50, "오른쪽", self.guide, " ", self.cable, prompt],
            ["007", 4, self.room, self.room, self.cable, "둘 다"],
        )
        self.xlsx.chmod(0o444)
        before = self.xlsx.read_bytes(), self.xlsx.stat().st_mtime_ns
        rows = read_workbook(self.xlsx)
        self.assertEqual([r.row_number for r in rows], [2, 4, 6, 7])
        self.assertEqual([r.display_number for r in rows], [30, 10, 50, 7])
        self.assertEqual([r.edit_id for r in rows], ["001", "왼쪽", "오른쪽", "4"])
        guides = [self.folder / self.guide] * 3 + [self.folder / self.room]
        self.assertEqual([row.guide for row in rows], guides)
        inputs = [
            [(ref.slot, ref.path.relative_to(self.folder).as_posix()) for ref in row.references]
            for row in rows
        ]
        expected = [[], [(1, self.room)], [(2, self.cable)], [(1, self.room), (2, self.cable)]]
        self.assertEqual(inputs, expected)
        self.assertEqual(rows[2].prompt, prompt)
        self.assertEqual((self.xlsx.read_bytes(), self.xlsx.stat().st_mtime_ns), before)

    def test_windows_relative_paths(self):
        self.write_fixture([1, "room", self.guide, r"사진\room.png", None, "방"])
        row, = read_workbook(self.xlsx)
        self.assertEqual(row.edit_id, "room")
        ref, = row.references
        self.assertEqual((ref.slot, ref.path), (1, self.folder / self.room))

    def test_only_the_numbered_six_column_layout_is_accepted(self):
        layouts = [
            (HEADERS[1:], "A1"),
            (("편집 ID", "가이드 이미지", "레퍼런스 이미지", "프롬프트"), "A1"),
            (("편집 ID", "번호", *HEADERS[2:]), "A1"),
            ((*HEADERS[:3], "레퍼런스 이미지", *HEADERS[4:]), "D1"),
            ((*HEADERS, "임의 열"), "G1"),
        ]
        for header, address in layouts:
            with self.subTest(header=header):
                self.write_fixture(headers=header)
                self.expect_error("편집!" + address, *HEADERS)

    def test_display_number_is_a_positive_integer(self):
        for raw in (0, -2, 1.5, True, "오번", "=ROW()-1"):
            with self.subTest(raw=raw):
                self.write_fixture([raw, "id", self.guide, None, None, "요청"])
                self.expect_error("편집!A2", "번호", repr(raw), "정수")

    def test_duplicate_id_and_missing_required_cells(self):
        row = [1, "x", self.guide, None, None, "요청"]
        self.write_fixture(row, [], [2, " x ", *row[2:]])
        self.expect_error("편집!B4", "편집 ID", "' x '", "중복", "2행")
        missing = [(0, None, "A3"), (1, " ", "B3"), (2, None, "C3"), (5, "   ", "F3")]
        for i, raw, address in missing:
            with self.subTest(address=address):
                data = row.copy()
                data[i] = raw
                self.write_fixture([], data)
                self.expect_error("편집!" + address, HEADERS[i], repr(raw), "필수")

    def test_bad_guides_and_references_report_cell_and_value(self):
        (self.folder / "broken.png").write_bytes(b"broken")
        (self.folder / "partial.png").write_bytes((self.folder / self.guide).read_bytes()[:40])
        Image.new("RGB", (8, 4)).save(self.folder / "fake.png", format="JPEG")
        cases = [
            (2, "missing.png", "C2", "없습니다"),
            (3, "broken.png", "D2", "입력 PNG"),
            (4, "partial.png", "E2", "입력 PNG"),
            (4, "fake.png", "E2", "입력 PNG"),
            (4, "wire.jpg", "E2", ".png"),
            (2, str(self.folder / self.guide), "C2", "상대 경로"),
            (2, r"C:\pics\guide.png", "C2", "상대 경로"),
        ]
        for index, raw, address, reason in cases:
            with self.subTest(value=raw):
                row = [1, "x", self.guide, None, None, "요청"]
                row[index] = raw
                self.write_fixture(row)
                self.expect_error("편집!" + address, HEADERS[index], repr(raw), reason)

    def test_missing_file_sheet_headers_and_no_requests(self):
        self.expect_error(str(self.xlsx), "편집", *HEADERS)
        self.xlsx.write_bytes(b"invalid workbook")
        self.expect_error(str(self.xlsx), "읽을 수 없습니다")
        self.write_fixture(title="다른 이름")
        self.expect_error(str(self.xlsx), "편집", *HEADERS)
        self.write_fixture(headers=HEADERS[:-1])
        self.expect_error("편집!F1", "프롬프트")
        self.write_fixture([])
        self.expect_error("편집", "편집 요청이 없습니다")

    def test_cli_prints_slots_and_exits_nonzero_for_an_invalid_row(self):
        args = ["workbook", str(self.xlsx)]
        self.write_fixture([42, "전선", self.guide, None, self.cable, "요청"])
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(args), 0)
        listing = stdout.getvalue()
        self.assertIn("편집 요청 1건", listing)
        self.assertIn("42번 전선 (엑셀 2행)", listing)
        self.assertIn("레퍼런스 2=" + self.cable, listing)
        self.assertNotIn("레퍼런스 1=", listing)
        self.write_fixture([42, "전선", self.guide, None, "missing.png", "요청"])
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(args), 1)
        self.assertIn("편집!E2", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
