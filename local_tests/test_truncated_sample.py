"""Length-truncated outputs leave a head/tail sample next to the model-call log (2026-10-09)."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from openviking.models import call_diagnostics as cd


def _response(reason, text):
    return SimpleNamespace(id="r", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=9),
                           choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=text))])


def test_only_length_finishes_are_sampled(tmp_path):
    cfg = SimpleNamespace(log=SimpleNamespace(model_calls_output=str(tmp_path / "model-calls.jsonl")))
    cd._sinks.clear()
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=cfg):
        obs = cd.CallObservation(model="m", input_tokens=1, max_tokens=9, tool_names=[], thinking=False, timeout=1)
        obs.complete(_response("stop", "fine"))
        obs.complete(_response("length", "H" * 600 + "x" * 5000 + "TAIL"))
    for sink in cd._sinks.values():
        for h in sink.handlers: h.flush()
    rows = [json.loads(l) for l in (tmp_path / "truncated-outputs.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["chars"] == 5604
    assert rows[0]["head"] == "H" * 500 and rows[0]["tail"].endswith("TAIL") and len(rows[0]["tail"]) == 2000
    assert len((tmp_path / "model-calls.jsonl").read_text().splitlines()) == 2
    cd._sinks.clear()


def test_cached_tokens_are_logged(tmp_path):
    cfg = SimpleNamespace(log=SimpleNamespace(model_calls_output=str(tmp_path / "model-calls.jsonl")))
    cd._sinks.clear()
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=cfg):
        obs = cd.CallObservation(model="m", input_tokens=1, max_tokens=9, tool_names=[], thinking=False, timeout=1)
        cached = _response("stop", "ok")
        cached.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=4408)
        obs.complete(cached)
        obs.complete(_response("stop", "ok"))  # provider without cache details
    for sink in cd._sinks.values():
        for h in sink.handlers: h.flush()
    rows = [json.loads(l) for l in (tmp_path / "model-calls.jsonl").read_text().splitlines()]
    assert [r["cached_tokens"] for r in rows] == [4408, None]
    cd._sinks.clear()
