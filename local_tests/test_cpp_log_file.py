"""The C++ spdlog sink truncates its file on open; it must not share the Python log file."""
from types import SimpleNamespace
from unittest.mock import patch

from openviking.storage.vectordb.utils import logging_init


def _init(output):
    seen = {}
    cfg = SimpleNamespace(log=SimpleNamespace(level="warning", output=output, format=None),
                          storage=SimpleNamespace(workspace="/tmp"))
    logging_init._cpp_logging_initialized = False
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=cfg), \
         patch("openviking.storage.vectordb.engine.init_logging",
               side_effect=lambda lvl, out, fmt: seen.setdefault("out", out)):
        logging_init.init_cpp_logging()
    logging_init._cpp_logging_initialized = False
    return seen["out"]


def test_file_output_goes_to_sibling_file():
    assert _init("/var/log/ov/openviking.log") == "/var/log/ov/openviking.vectordb.log"


def test_streams_unchanged():
    assert _init("stderr") == "stderr"
