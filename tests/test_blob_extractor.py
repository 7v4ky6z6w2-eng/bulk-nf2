import io

from PIL import Image

from app.images.blob_extractor import extract_image


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buf, format="PNG")
    return buf.getvalue()


def test_extract_image_valid_png():
    result = extract_image(_png_bytes(), "REF001")
    assert result is not None
    assert result.mime_type == "image/png"
    assert result.filename == "REF001.png"


def test_extract_image_none_when_empty():
    assert extract_image(None, "REF001") is None
    assert extract_image(b"", "REF001") is None


def test_extract_image_none_when_not_an_image():
    assert extract_image(b"this is definitely not an image", "REF001") is None


def test_extract_image_sanitizes_filename():
    result = extract_image(_png_bytes(), "REF/001 *weird*")
    assert result.filename == "REF001weird.png"
