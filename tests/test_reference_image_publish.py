from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from workflow_1256.reference_image_publish import publish_local_reference_images, split_reference_inputs


def test_local_image_is_published_through_injected_publisher() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "ref.png"
        path.write_bytes(b"image-content")
        calls = []
        def publisher(content, filename, metadata):
            calls.append((content, filename, metadata))
            return {"url": "https://signed.example/ref.png"}
        assert publish_local_reference_images([str(path)], publisher=publisher) == ["https://signed.example/ref.png"]
    assert calls[0][0] == b"image-content"
    assert calls[0][2]["mime_type"] == "image/png"


def test_split_keeps_urls_and_rejects_unknown_file() -> None:
    remote, local = split_reference_inputs(["https://example.com/ref.png"])
    assert remote == ["https://example.com/ref.png"]
    assert local == []
    with pytest.raises(Exception):
        split_reference_inputs(["C:/not/a/real/image.png"])
