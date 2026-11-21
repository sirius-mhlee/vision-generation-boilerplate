"""Read and validate the single-sheet vgen input without loading a model."""

import zlib
from contextlib import closing
from dataclasses import dataclass, field
from io import BytesIO
from itertools import zip_longest
from pathlib import Path, PureWindowsPath
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from PIL import Image

from .png import verified_png_sha256

SHEET = "편집"
HEADERS = (
    "번호", "편집 ID", "가이드 이미지", "레퍼런스 이미지 1", "레퍼런스 이미지 2", "프롬프트",
)
NUMBER, ID, GUIDE, REF1, REF2, PROMPT = HEADERS


class WorkbookError(ValueError):
    """An input error with a location suitable for the workbook author."""


@dataclass(frozen=True)
class Reference:
    slot: int
    path: Path
    sha256: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class EditRow:
    row_number: int
    display_number: int
    edit_id: str
    guide: Path
    references: tuple[Reference, ...]
    prompt: str
    guide_sha256: str | None = field(default=None, compare=False, kw_only=True)


def _check_headers(cells) -> None:
    for column, (expected, cell) in enumerate(zip_longest(HEADERS, cells), start=1):
        raw = cell.value if cell is not None else None
        label = (raw.strip() or None) if isinstance(raw, str) else raw
        if label != expected:
            address = f"{SHEET}!{get_column_letter(column)}1"
            raise WorkbookError(
                f"{address} (값={raw!r}): 첫 행은 A열부터 {', '.join(HEADERS)} 순서로 작성하세요."
            )


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _display_number(value) -> int:
    if _blank(value):
        raise ValueError("필수 값이 비어 있습니다.")
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or value < 1:
        raise ValueError("번호에는 1 이상의 정수를 입력하세요.")
    return value


def _text(name: str, cell) -> str:
    value = cell.value if cell is not None else None
    if _blank(value):
        if name in (ID, GUIDE, PROMPT):
            raise ValueError("필수 값이 비어 있습니다.")
        return ""
    if name == ID and cell.data_type == "n":
        return str(value)
    if cell.data_type in ("f", "e") or not isinstance(value, str):
        raise ValueError("셀에 텍스트 값을 입력하세요.")
    value = value if name == PROMPT else value.strip()
    if name == ID and (value in (".", "..") or PureWindowsPath(value).name != value):
        raise ValueError("파일명으로 쓸 수 있는 ID를 입력하세요.")
    return value


def _image_path(value: str, folder: Path, verified: dict[Path, str], max_edge: int | None) -> Path:
    if PureWindowsPath(value).anchor:
        raise ValueError("엑셀 폴더 기준의 상대 경로를 입력하세요.")
    relative = Path(value.replace("\\", "/"))
    if relative.suffix.lower() != ".png":
        raise ValueError("확장자가 .png인 PNG 사진을 지정하세요.")
    path = folder / relative
    if path not in verified:
        try:
            # Validate and fingerprint the same bytes even if the path is replaced during prepare.
            with BytesIO(path.read_bytes()) as source:
                verified[path] = verified_png_sha256(source, max_edge=max_edge)
        except FileNotFoundError as exc:
            raise ValueError(f"이미지 파일이 없습니다. {path}") from exc
        except (
            OSError, EOFError, SyntaxError, ValueError, zlib.error, Image.DecompressionBombError,
        ) as exc:
            raise ValueError(f"입력 PNG를 확인하세요. {path} ({exc})") from exc
    return path


def _read_sheet(sheet, folder: Path, max_edge: int | None) -> list[EditRow]:
    # Excel writers can omit or misreport the used range. Read the actual cells.
    sheet.reset_dimensions()
    rows = sheet.iter_rows()
    _check_headers(next(rows, ()))
    verified: dict[Path, str] = {}
    first_rows: dict[str, int] = {}
    edits = []

    for row_number, cells in enumerate(rows, start=2):
        if all(_blank(cell.value) for cell in cells):
            continue

        texts = {}
        images = {}
        for index, name in enumerate(HEADERS):
            cell = cells[index] if index < len(cells) else None
            raw = cell.value if cell is not None else None
            try:
                if name == NUMBER:
                    display_number = _display_number(raw)
                    continue
                value = _text(name, cell)
                if name == ID and value in first_rows:
                    raise ValueError(f"중복 ID입니다. 처음 나온 행은 {first_rows[value]}행입니다.")
                if name in (ID, PROMPT):
                    texts[name] = value
                elif value:
                    images[name] = _image_path(value, folder, verified, max_edge)
            except ValueError as exc:
                address = f"{SHEET}!{get_column_letter(index + 1)}{row_number}"
                raise WorkbookError(f"{address} ({name}, 값={raw!r}): {exc}") from exc
        references = tuple(
            Reference(slot, images[name], sha256=verified[images[name]])
            for slot, name in enumerate((REF1, REF2), start=1)
            if name in images
        )
        guide = images[GUIDE]
        edits.append(EditRow(row_number, display_number, texts[ID], guide, references,
                             texts[PROMPT], guide_sha256=verified[guide]))
        first_rows[texts[ID]] = row_number
    if not edits:
        raise WorkbookError(f"{SHEET} 시트에 편집 요청이 없습니다. 2행부터 입력하세요.")
    return edits


def read_workbook(filename: str | Path, *, max_edge: int | None = None) -> list[EditRow]:
    """Read edits in sheet order. With max_edge, also verify each planned FLUX input size."""
    if max_edge is not None and (type(max_edge) is not int or not 16 <= max_edge <= 1024):
        raise WorkbookError("--max-edge는 16~1024 px 범위의 정수로 지정하세요.")
    path = Path(filename).absolute()
    layout = f"필요한 시트는 '{SHEET}'이며 첫 행의 열은 {', '.join(HEADERS)}입니다."
    try:
        with path.open("rb") as stream, closing(
            load_workbook(stream, read_only=True, keep_links=False)
        ) as book:
            if SHEET not in {sheet.title for sheet in book.worksheets}:
                raise WorkbookError(f"{path}: {layout}")
            return _read_sheet(book[SHEET], path.parent, max_edge)
    except WorkbookError:
        raise
    except FileNotFoundError as exc:
        raise WorkbookError(f"엑셀 파일이 없습니다. {path}\n{layout}") from exc
    except (
        OSError, BadZipFile, InvalidFileException, ParseError, KeyError, IndexError,
        TypeError, ValueError, zlib.error,
    ) as exc:
        raise WorkbookError(f"엑셀 파일을 읽을 수 없습니다. {path} ({exc})") from exc
