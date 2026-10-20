"""Pi/Anthropic JSON image blocks are redacted before WM/long-term extraction (upstream PR #5812)."""
from openviking.session import working_memory as wm


def test_json_image_blocks_redacted():
    png, jpeg = "iVBORw0KGgo" + "A" * 50_000, "/9j/" + "B" * 30_000
    text = (
        '[{"type":"text","text":"Read image file"},{"type":"image","data":"' + png + '"},'
        '{"type":"image","source":{"type":"base64","data": "' + jpeg + '"}}]'
    )
    out = wm.redact_inline_images(text)
    assert "mime=image/png, base64_chars=50011" in out
    assert "mime=image/jpeg, base64_chars=30004" in out
    assert png not in out and jpeg not in out and "Read image file" in out


def test_non_image_data_field_kept():
    text = '{"data":"aGVsbG8gd29ybGQ=","rows":[{"data":"plain"}]}'
    assert wm.redact_inline_images(text) == text
