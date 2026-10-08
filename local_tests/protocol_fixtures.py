# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for JSON and restricted-Python memory extraction output protocols."""

import json
from types import SimpleNamespace
from unittest.mock import patch


from openviking.session.memory.dataclass import MemoryField, MemoryFile, MemoryTypeSchema
from openviking.session.memory.extraction_output_protocol import (
    ExtractionOutputContext,
    create_extraction_output_protocol,
)
from openviking.session.memory.memory_isolation_handler import RoleScope
from openviking.session.memory.merge_op import FieldType, MergeOp
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.schema_model_generator import SchemaModelGenerator


def _preference_schema(*, operation_mode: str = "upsert") -> MemoryTypeSchema:
    return MemoryTypeSchema(
        memory_type="preferences",
        description="User preferences",
        directory="viking://user/{{ user_space }}/memories/preferences",
        filename_template="{{ topic }}.md",
        operation_mode=operation_mode,
        fields=[
            MemoryField(
                name="topic",
                field_type=FieldType.STRING,
                merge_op=MergeOp.IMMUTABLE,
            ),
            MemoryField(
                name="content",
                field_type=FieldType.STRING,
                merge_op=MergeOp.PATCH,
            ),
            MemoryField(
                name="score",
                field_type=FieldType.INT64,
                merge_op=MergeOp.SUM,
            ),
        ],
    )


def _project_schema() -> MemoryTypeSchema:
    return MemoryTypeSchema(
        memory_type="projects",
        description="Projects",
        directory="viking://user/{{ user_space }}/memories/projects",
        filename_template="{{ name }}.md",
        fields=[
            MemoryField(
                name="name",
                field_type=FieldType.STRING,
                merge_op=MergeOp.IMMUTABLE,
            ),
            MemoryField(
                name="content",
                field_type=FieldType.STRING,
                merge_op=MergeOp.REPLACE,
            ),
        ],
    )


def _profile_schema() -> MemoryTypeSchema:
    return MemoryTypeSchema(
        memory_type="profile",
        description="User profile",
        directory="viking://user/{{ user_space }}/memories",
        filename_template="profile.md",
        fields=[
            MemoryField(
                name="content",
                field_type=FieldType.STRING,
                merge_op=MergeOp.PATCH,
            )
        ],
    )


def _context(
    schemas: list[MemoryTypeSchema],
    *,
    files: list[MemoryFile] | None = None,
    link_enabled: bool = False,
    role_scope: RoleScope | None = None,
    available_tools: tuple[str, ...] = ("read",),
) -> ExtractionOutputContext:
    config = SimpleNamespace(memory=SimpleNamespace(link_enabled=link_enabled))
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=config):
        operations_model = SchemaModelGenerator(schemas).create_structured_operations_model(
            role_scope
        )
    page_id_map = PageIdMap()
    read_file_contents = {}
    for memory_file in files or []:
        read_file_contents[memory_file.uri] = memory_file
        page_id_map.get_page_id(memory_file.uri)
    return ExtractionOutputContext(
        operations_model=operations_model,
        schemas=tuple(schemas),
        page_id_map=page_id_map,
        read_file_contents=read_file_contents,
        link_enabled=link_enabled,
        role_scope=role_scope,
        available_tools=available_tools,
    )


def _existing_preference(uri: str, topic: str, content: str, score: int = 0) -> MemoryFile:
    return MemoryFile(
        uri=uri,
        memory_type="preferences",
        content=content,
        extra_fields={
            "topic": topic,
            "score": score,
            "version": 7,
            "_uri": uri,
        },
    )


def _bind(protocol, context: ExtractionOutputContext) -> str:
    protocol.render_contract(context)
    return protocol.render_new_bindings(context, source="test read")


