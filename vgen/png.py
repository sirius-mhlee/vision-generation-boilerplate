"""PNG validation for input photos and generated files."""

from hashlib import file_digest
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps


def fit_model_input(image: Image.Image, max_edge: int, resample: Image.Resampling) -> tuple[int, int]:
    """Thumbnail an oriented image in place and return its padded FLUX dimensions."""
    image.thumbnail((max_edge, max_edge), resample)
    width = image.width + (-image.width % 16)
    height = image.height + (-image.height % 16)
    short, long = sorted((width, height))
    if short < 64 or long > 8 * short:
        raise ValueError(
            f"--max-edge {max_edge} 적용 후 모델 입력이 {width}×{height} px입니다. "
            "짧은 변이 64 px 이상이고 긴 변이 짧은 변의 8배 이하여야 합니다."
        )
    return width, height


def verified_png_sha256(stream: BinaryIO, *, max_edge: int | None = None) -> str:
    """Verify the PNG, optionally check its planned model dimensions, and hash its bytes."""
    with Image.open(stream, formats=("PNG",)) as image:
        image.verify()
    stream.seek(0)
    with Image.open(stream, formats=("PNG",)) as image:
        image.load()
        if max_edge is not None:
            ImageOps.exif_transpose(image, in_place=True)
            # Only the thumbnail dimensions matter during planning.
            fit_model_input(image, max_edge, Image.Resampling.NEAREST)
    stream.seek(0)
    return file_digest(stream, "sha256").hexdigest()


def matches_saved_png(path: Path, expected: str | None) -> bool:
    """Compare a previously verified output with its recorded success."""
    if expected is None or path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != expected:
            return False
        stream.seek(0)
        try:
            with Image.open(stream, formats=("PNG",)) as image:
                image.verify()
        except (OSError, EOFError, SyntaxError, ValueError, Image.DecompressionBombError):
            return False
    return True
