"""0.4.23 resolves the VLM per account (Session._get_vlm_config). These tests
stub the process-wide config instead, so route the account lookup to it."""
import pytest

import openviking.session.session as session_module


@pytest.fixture(autouse=True)
def _account_vlm_from_global_config(monkeypatch):
    async def _get_vlm_config(self):
        return session_module.get_openviking_config().vlm

    monkeypatch.setattr(session_module.Session, "_get_vlm_config", _get_vlm_config)
