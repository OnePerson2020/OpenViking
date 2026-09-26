# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Direct Session evidence for the canonical Case's fixed Experience target."""

from __future__ import annotations

import json
from typing import Any

from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.tools import add_tool_call_pair_to_messages


class AgentExperienceContextProvider(SessionExtractContextProvider):
    """Read exactly one Experience and propose its complete replacement DAG."""

    include_tool_parts_in_conversation = True

    def __init__(
        self,
        messages: Any,
        *,
        case: Any,
        target_uri: str,
        source_session_uri: str,
        evaluation: Any = None,
        dag_execution: dict[str, Any] | None = None,
        vlm_config: Any = None,
        memory_registry: Any = None,
    ):
        super().__init__(messages=messages, vlm_config=vlm_config, memory_registry=memory_registry)
        self.case = case
        self.target_uri = target_uri
        self.source_session_uri = source_session_uri
        self.evaluation = evaluation
        self.dag_execution = dict(dag_execution or {})

    def instruction(self) -> str:
        return f"""Reflect on the supplied Session and update its canonical Case's Experience.
The fixed experience_name is {json.dumps(self.case.name, ensure_ascii=False)}.
The only writable target is {self.target_uri}.

Output exactly ONE experience entry with this exact name, or no operations if no useful
change is supported. Do not rename, delete, supersede, split into smaller Experiences,
or update another Case. Keep all dependent steps and conditional paths of this Case in
one complete standalone Python DAG. Emit restricted Python memory SDK code only.
Preserve unaffected node variable names and paths from the existing DAG. Generalize
instance-specific identifiers. Ground tool nodes in the Session's actual tools.

Use the raw conversation, tool results, evaluation feedback and observed Experience
execution to find the first incorrect decision or missing obligation. Runtime slot values
are observations, not business-success proof. A failed Session must remain incomplete at
its failed obligation and receive an actionable correction; never make it look successful
by weakening checks or deleting obligations. Preserve successful paths.
An absent evaluation means UNKNOWN, not success. Reflect on the evidence without inventing
an external verdict. The replay Gate will independently compare baseline and candidate.
Keep the exact Case name even if its language differs. Write DAG descriptions in
{self._output_language}."""

    def get_memory_schemas(self, ctx: Any) -> list[Any]:
        schema = self._get_registry().get("experiences")
        return [schema] if schema is not None and schema.enabled else []

    def get_tools(self) -> list[str]:
        return []

    async def prefetch(self) -> list[dict[str, Any]]:
        from dataclasses import asdict

        messages = [self._build_conversation_message()]
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "case": asdict(self.case),
                        "source_session_uri": self.source_session_uri,
                        "target_experience_uri": self.target_uri,
                        "evaluation": asdict(self.evaluation)
                        if self.evaluation is not None
                        else None,
                        "experience_execution": self.dag_execution,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
        result = await self.read_file(self.target_uri)
        if result is not None:
            add_tool_call_pair_to_messages(
                messages=messages,
                call_id="case-experience",
                tool_name="read",
                params={"uri": self.target_uri},
                result=result,
            )
        messages.append(
            {
                "role": "user",
                "content": "Propose the complete DAG for the fixed Case Experience using this Session. "
                "Return restricted Python memory SDK code.",
            }
        )
        return messages
