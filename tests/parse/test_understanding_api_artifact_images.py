import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from openviking.parse.image_rewrite import (
    IMAGE_MAPPINGS_FILENAME,
    build_artifact_image_mappings,
)
from openviking.parse.understanding_api import UnderstandingAPI


class _FakeVikingFS:
    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.deleted_temps = []

    def create_temp_uri(self):
        return "viking://temp/artifact"

    async def mkdir(self, uri, exist_ok=False):
        self.dirs.add(uri)

    async def write_file_bytes(self, uri, content):
        self.files[uri] = content

    async def write_file(self, uri, content):
        self.files[uri] = content.encode("utf-8")

    async def delete_temp(self, uri):
        self.deleted_temps.append(uri)


def test_build_artifact_image_mappings_uses_existing_sibling_images(tmp_path: Path):
    chapter = tmp_path / "章节"
    chapter.mkdir()
    (chapter / "正文_img1.png").write_bytes(b"png")
    (chapter / "正文_img2.jpg").write_bytes(b"jpg")
    (chapter / "正文.md").write_text(
        "\n".join(
            [
                "![image](正文_img1.png)",
                '<img src="./正文_img2.jpg">',
                "![remote](https://example.com/a.png)",
                "![missing](missing.png)",
                "```markdown",
                "![example](正文_img1.png)",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    assert build_artifact_image_mappings(tmp_path) == {
        "章节/正文.md": {
            "正文_img1.png": "正文_img1.png",
            "./正文_img2.jpg": "正文_img2.jpg",
        }
    }


@pytest.mark.asyncio
async def test_unpack_artifact_writes_image_mapping_sidecar(tmp_path: Path):
    zip_path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("artifact/Ov测试_1.md", "![image](Ov测试_1_img1.png)\n")
        archive.writestr("artifact/Ov测试_1_img1.png", b"png")
        archive.writestr(
            f"artifact/{IMAGE_MAPPINGS_FILENAME}",
            '{"untrusted.md":{"bad.png":"bad.png"}}',
        )

    fake_fs = _FakeVikingFS()
    api = UnderstandingAPI.__new__(UnderstandingAPI)
    with patch("openviking.parse.understanding_api.get_viking_fs", return_value=fake_fs):
        temp_uri = await api._unpack_zip_to_temp_dir(zip_path, "resource")

    assert temp_uri == "viking://temp/artifact"
    sidecar_uri = f"{temp_uri}/resource/{IMAGE_MAPPINGS_FILENAME}"
    assert json.loads(fake_fs.files[sidecar_uri]) == {
        "Ov测试_1.md": {"Ov测试_1_img1.png": "Ov测试_1_img1.png"}
    }
    assert fake_fs.files[f"{temp_uri}/resource/Ov测试_1_img1.png"] == b"png"


@pytest.mark.asyncio
async def test_unpack_artifact_cleans_temp_on_failure(tmp_path: Path):
    invalid_zip = tmp_path / "invalid.zip"
    invalid_zip.write_bytes(b"not-a-zip")
    fake_fs = _FakeVikingFS()
    api = UnderstandingAPI.__new__(UnderstandingAPI)

    with (
        patch("openviking.parse.understanding_api.get_viking_fs", return_value=fake_fs),
        pytest.raises(zipfile.BadZipFile),
    ):
        await api._unpack_zip_to_temp_dir(invalid_zip, "resource")

    assert fake_fs.deleted_temps == ["viking://temp/artifact"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["step", "stp", "dwg"])
async def test_cad_plain_zip_uses_generic_materialization(tmp_path, monkeypatch, extension):
    from unittest.mock import AsyncMock

    source = tmp_path / ("model." + extension)
    source.write_bytes(b"ISO-10303-21;" if extension != "dwg" else b"AC1032")
    contents = {
        source.name: source.read_bytes(),
        "evidence.json": b'{"schema":"openviking.step-evidence/v1"}',
        **{
            name + ".png": b"png"
            for name in (
                "front",
                "top",
                "right",
                "iso_front_top",
                "iso_back_top",
                "iso_front_bottom",
            )
        },
    }
    archive_path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in contents.items():
            archive.writestr("model_step/" + name, data)
    api = UnderstandingAPI.__new__(UnderstandingAPI)
    api._video_exts = api._audio_exts = api._image_exts = set()
    api._create_response_for_file = AsyncMock(return_value={"id": "response-1"})
    api._poll_response = AsyncMock(
        return_value={"result": {"zip_url": "https://example.test/result.zip"}}
    )
    api._download_zip = AsyncMock(return_value=archive_path)
    fs = _FakeVikingFS()
    monkeypatch.setattr("openviking.parse.understanding_api.get_viking_fs", lambda: fs)
    result = await api.parse(source, understanding_file_id="file-1")
    assert result.source_format == ("step" if extension == "stp" else extension)
    assert "cad_artifact" not in result.meta
    assert fs.files == {
        "viking://temp/artifact/model/" + name: data for name, data in contents.items()
    }
    assert not archive_path.exists()
