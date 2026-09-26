from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart
from openviking.session.memory.agent_experience_context_provider import (
    AgentExperienceContextProvider,
)
from openviking.session.train import Case, Rubric


@pytest.mark.asyncio
async def test_prefetch_reads_only_fixed_case_experience():
    uri = "viking://user/u/memories/experiences/case_a.md"
    case = Case(name="case_a", task_signature="cancel", input={}, rubric=Rubric("r", "", []))
    provider = AgentExperienceContextProvider(
        messages=[Message(id="m", role="user", parts=[TextPart(text="cancel duplicate")])],
        case=case,
        target_uri=uri,
        source_session_uri="viking://session/s/archives/1",
    )
    provider.read_file = AsyncMock(return_value={"content": "dag = workflow('cancel')"})
    provider.search_files = AsyncMock(
        side_effect=AssertionError("must not search other Experiences")
    )
    result = await provider.prefetch()
    provider.read_file.assert_awaited_once_with(uri)
    provider.search_files.assert_not_called()
    text = json.dumps(result, ensure_ascii=False)
    assert "cancel duplicate" in text and "case_a" in text
    assert "trajector" not in text.lower()
    assert "exact name" in provider.instruction()
    assert provider.get_tools() == []
