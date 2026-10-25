"""Local: agent-memory extraction sees the head and tail of long tool outputs."""
from openviking.message import Message
from openviking.message.part import ToolPart
from openviking.session.memory.agent_trajectory_context_provider import (
    AgentTrajectoryContextProvider,
)
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider


def _messages(output):
    part = ToolPart(tool_id="t1", tool_name="bash", tool_output=output, tool_status="error")
    return [Message(id="1", role="assistant", parts=[part])]


def test_long_output_keeps_head_and_tail():
    output = "START " + "x" * 10000 + " Traceback: KeyError foo"
    text = AgentTrajectoryContextProvider(messages=[])._assemble_conversation(_messages(output))
    assert "START" in text and "KeyError foo" in text
    assert "chars omitted" in text and len(text) < 2200


def test_short_output_is_whole_and_user_path_still_drops_tools():
    msgs = _messages("small result")
    assert "output=small result" in AgentTrajectoryContextProvider(messages=[])._assemble_conversation(msgs)
    assert SessionExtractContextProvider(messages=[])._assemble_conversation(msgs) == ""


def test_long_input_keeps_head_and_tail():
    part = ToolPart(tool_id="t1", tool_name="bash", tool_input={"command": "BEGIN " + "y" * 10000 + " END"})
    text = AgentTrajectoryContextProvider(messages=[])._assemble_conversation(
        [Message(id="1", role="assistant", parts=[part])]
    )
    assert "BEGIN" in text and "END" in text and "chars omitted" in text and len(text) < 1200
