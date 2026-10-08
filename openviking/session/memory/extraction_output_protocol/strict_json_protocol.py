# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Provider-constrained JSON actions; no Python, JSON repair or partial acceptance."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from openviking.models.vlm.base import ToolCall, VLMResponse
from openviking.session.memory.extraction_output_protocol.json_protocol import JsonExtractionOutputProtocol


class StrictActionError(ValueError):
    """Safe, actionable diagnostics: codes/known schema fields, never response text."""
    def __init__(self, code, message, path=""):
        self.code = code
        self.path = path
        super().__init__(f"{code}" + (f" path={path}" if path else "") + f": {message}")


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Close objects and require their declared fields without weakening value types.

    Defaults must be emitted explicitly. Optional nullable fields stay nullable.
    Open-ended dictionaries cannot be represented safely and are rejected.
    """
    result = deepcopy(schema)
    def visit(node):
        if not isinstance(node, dict):
            return
        node.pop('default', None)
        if node.get('type') == 'object' or 'properties' in node:
            if node.get('additionalProperties') not in (None, False):
                raise ValueError('Strict extraction does not support open-ended objects')
            properties = node.get('properties', {})
            node['additionalProperties'] = False
            node['required'] = list(properties)
            for value in properties.values():
                visit(value)
        for keyword in ('$defs', 'definitions'):
            for value in node.get(keyword, {}).values():
                visit(value)
        for keyword in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            for value in node.get(keyword, []):
                visit(value)
        if isinstance(node.get('items'), dict):
            visit(node['items'])
    visit(result)
    Draft202012Validator.check_schema(result)
    return result


def strict_loads(content: str) -> Any:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise StrictActionError('JSON_DUPLICATE_KEY', 'Each JSON object key must occur once')
            result[key] = value
        return result
    def constant(_):
        raise StrictActionError('JSON_NONFINITE', 'JSON numbers must be finite')
    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


class StrictJsonExtractionOutputProtocol(JsonExtractionOutputProtocol):
    name = 'json_schema'

    def response_format(self, context, tools):
        operations = strict_schema(context.operations_model.model_json_schema())
        definitions = operations.pop('$defs', {})
        branches = [{'type': 'object', 'properties': {'operations': operations},
                     'required': ['operations'], 'additionalProperties': False}]
        tool_branches = []
        for tool in tools:
            fn = tool['function']
            if fn['name'] not in {'read', 'search', 'ls'}:
                raise ValueError('Strict extraction only permits read-only memory tools')
            parameters = deepcopy(fn['parameters'])
            required = parameters.get('required', [])
            for name, field in list(parameters.get('properties', {}).items()):
                if name not in required:
                    parameters['properties'][name] = {'anyOf': [field, {'type': 'null'}]}
            tool_branches.append({'type': 'object', 'properties': {
                'name': {'type': 'string', 'enum': [fn['name']]},
                'arguments': strict_schema(parameters)},
                'required': ['name', 'arguments'], 'additionalProperties': False})
        if tool_branches:
            branches.append({'type': 'object', 'properties': {'tool_calls': {
                'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': {'anyOf': tool_branches}}},
                'required': ['tool_calls'], 'additionalProperties': False})
        schema = {'type': 'object', 'properties': {'action': {'anyOf': branches}},
                  'required': ['action'], 'additionalProperties': False}
        if definitions:
            schema['$defs'] = definitions
        Draft202012Validator.check_schema(schema)
        return {'type': 'json_schema', 'json_schema': {
            'name': 'memory_extraction_action', 'strict': True, 'schema': schema}}

    def parse_response(self, response, context, response_format, tools):
        if not isinstance(response, VLMResponse):
            raise StrictActionError('RESPONSE_METADATA_MISSING', 'Structured response metadata is required')
        if response.finish_reason != 'stop' or response.has_tool_calls:
            raise StrictActionError('RESPONSE_NOT_COMPLETE', 'Return complete JSON with stop; native tool wrappers are not accepted')
        if not isinstance(response.content, str):
            raise StrictActionError('RESPONSE_CONTENT_MISSING', 'JSON string content is required')
        raw = strict_loads(response.content)
        Draft202012Validator(response_format['json_schema']['schema']).validate(raw)
        action = raw['action']
        if 'tool_calls' in action:
            allowed = {tool['function']['name']: tool['function']['parameters'] for tool in tools}
            calls = []
            for call in action['tool_calls']:
                original = allowed[call['name']]
                # Explicit null only means omit an OPTIONAL tool argument, not a
                # business field. Validate original tool schema before execution.
                args = {k: v for k, v in call['arguments'].items()
                        if v is not None or k in original.get('required', [])}
                Draft202012Validator(original).validate(args)
                calls.append(ToolCall(id='structured_' + uuid4().hex,
                                      name=call['name'], arguments=args))
            return calls, None
        operations = action['operations']
        # Validate complete original business schema before Pydantic's compatibility
        # validators can fill defaults/coerce or silently ignore malformed fields.
        Draft202012Validator(context.operations_model.model_json_schema()).validate(operations)
        self.validate_business_shape(operations, context)
        model = context.operations_model.model_validate_json(json.dumps(operations), strict=True)
        return None, model

    @staticmethod
    def validate_business_shape(operations, context):
        """Preserve the Python protocol's complete-create and safe-delete boundary."""
        seen = set()
        singleton_targets = set()
        by_type = {s.memory_type: s for s in context.schemas}
        for name, schema in by_type.items():
            for item in operations.get(name, []):
                page_id = item['page_id']
                if page_id in seen:
                    raise StrictActionError('DUPLICATE_PAGE_ID', 'Combine changes for an existing page or assign distinct new page IDs', name)
                seen.add(page_id)
                uri = context.page_id_map.resolve(page_id)
                if schema.operation_mode == 'add_only' and uri:
                    raise StrictActionError('ADD_ONLY_EXISTING_PAGE', 'Use a new unregistered page_id >=100 for add-only memories', name)
                if not uri:
                    if page_id < 100:
                        raise StrictActionError('NEW_PAGE_ID_RANGE', 'New memory page_id must be at least 100', name)
                    for field in schema.fields:
                        # Presence is required. Nullability is decided by the
                        # already-validated original schema, just as in the Python
                        # compiler (e.g. supersedes=null means no replaced experience).
                        if field.name not in item:
                            raise StrictActionError('NEW_FIELD_MISSING', 'Supply every new-memory field; nullable fields may be null', name + '.' + field.name)
                        if isinstance(item[field.name], dict) and 'blocks' in item[field.name]:
                            raise StrictActionError('NEW_VALUE_IS_PATCH', 'New memories need complete values, not patch blocks', name + '.' + field.name)
                if not schema.filename_has_variables():
                    target = (name, item.get('peer_id'))
                    if target in singleton_targets:
                        raise StrictActionError('DUPLICATE_SINGLETON', 'Combine changes into one operation per singleton target', name)
                    singleton_targets.add(target)
        for deletion in operations.get('delete_ids', []):
            uri = context.page_id_map.resolve(deletion['delete_page_id'])
            memory = context.read_file_contents.get(uri)
            if memory is None:
                raise StrictActionError('DELETE_UNREAD', 'Read the existing target before proposing its deletion', 'delete_ids')
            resolver = context.memory_type_resolver
            memory_type = resolver(uri) if callable(resolver) else memory.memory_type
            if callable(resolver) and memory.memory_type and memory_type != memory.memory_type:
                raise StrictActionError('DELETE_TYPE_CONFLICT', 'Target metadata and authorized schema path disagree', 'delete_ids')
            schema = by_type.get(memory_type)
            if schema is None or schema.operation_mode == 'add_only':
                raise StrictActionError('DELETE_TYPE_NOT_ALLOWED', 'Only an unambiguously identified, deletable allowed type may be deleted', 'delete_ids')
            if deletion['delete_page_id'] in seen:
                raise StrictActionError('DELETE_UPDATE_CONFLICT', 'Do not update and delete the same page', 'delete_ids')
            replacement = deletion.get('replacement_page_id')
            if replacement is not None and replacement not in seen and not context.page_id_map.resolve(replacement):
                raise StrictActionError('DELETE_REPLACEMENT_UNKNOWN', 'Reference an existing or newly declared replacement page', 'delete_ids')

    def _field_scoped_result(self, result, context):
        if not isinstance(result, dict):
            return result
        page_id = result.get('page_id')
        uri = context.page_id_map.resolve(page_id) if page_id is not None else result.get('_read_uri')
        resolver = context.memory_type_resolver
        name = resolver(uri) if uri and callable(resolver) else result.get('memory_type')
        schema = next((s for s in context.schemas if s.memory_type == name), None)
        if schema is None or any(f.name == 'content' for f in schema.fields):
            return result
        scoped = dict(result)
        # A rendered multi-field body is not a writable `content` field. Showing
        # it beside real fields caused anchors from core_truths to target continuity.
        # Remove only this duplicate rendering from model context, never source data.
        scoped.pop('content', None)
        scoped['_field_scope_rule'] = (
            'This memory has NO writable content field. Edit each named field only '
            'using original text from that SAME field. The combined rendered body '
            'is not a field value. Use read(uri, field=<name>, text_offset=...) to '
            'inspect omitted text. Never copy a core_truths anchor into continuity '
            'or boundaries. Leave unchanged nullable fields null.')
        scoped['_writable_fields'] = [f.name for f in schema.fields]
        return scoped

    def render_tool_result_messages(self, context, *, result, **kwargs):
        return super().render_tool_result_messages(
            context, result=self._field_scoped_result(result, context), **kwargs)

    def render_prefetch_messages(self, messages, context):
        result = []
        for message in messages:
            content = message.get('content')
            if message.get('role') != 'user' or not isinstance(content, str):
                result.append(message); continue
            try:
                payload = json.loads(content)
            except ValueError:
                result.append(message); continue
            if isinstance(payload, dict) and payload.get('tool_call_name') == 'read' and isinstance(payload.get('result'), dict):
                payload['result'] = self._field_scoped_result(payload['result'], context)
                result.append({**message, 'content': json.dumps(payload, ensure_ascii=False)})
            else:
                result.append(message)
        return result

    def render_contract(self, context):
        return (
            '## Output Format: strict JSON memory actions\n'
            'Return exactly one JSON object matching the response schema; never Python code, '
            'Markdown fences or prose outside JSON. Choose exactly one action:\n'
            '- {"action":{"tool_calls":[{"name":"read|search|ls","arguments":{...}}]}} '
            'to request permitted read-only tools (at most 8). Use null for optional tool '
            'arguments to retain their default behavior.\n'
            '- {"action":{"operations":{...}}} for the final memory changes. '
            'Emit every operations field, using [] for unchanged memory types. Emit null '
            'for unchanged nullable business fields ONLY on existing pages. New memories must '
            'include every business field using full values (never patch blocks); null is permitted '
            'only where the schema allows it. For new experiences with no replacement, supersedes '
            'may be null or an empty string; never invent a replacement. '
            'Do not manufacture empty changes on error.\n'
            'Use exact SEARCH/REPLACE or DELETE blocks for partial updates; all existing '
            'page_id, ownership, ranges and partial-read rules still apply. '
            'The complete operations schema is:\n' +
            json.dumps(strict_schema(context.operations_model.model_json_schema()), ensure_ascii=False)
        )

    def parse(self, content, context):
        # Interface compatibility only; the live path also validates response metadata.
        try:
            fmt = self.response_format(context, [])
            response = VLMResponse(content=content, finish_reason='stop')
            return self.parse_response(response, context, fmt, [])[1], None
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            return None, type(exc).__name__

    def render_final_instruction(self, context):
        del context
        return ('Return only {"action":{"operations":{...}}} matching the response schema. '
                'No more tools are available for this iteration. No Python or Markdown fences.')

    def render_format_retry(self, error=None):
        return ('Your previous response failed strict validation: ' + str(error or 'invalid action') +
                '. Return one complete schema-valid JSON action, not Python or Markdown. '
                'Do not replace intended changes with an empty result to suppress an error.')

    def render_patch_repair(self, patch_errors):
        return super().render_patch_repair(patch_errors) + (
            '\nIf found_in_other_fields is present, the proposed anchor belongs to a '
            'different field of the same memory. Use the separately labeled exact-field '
            'reads below to regenerate the proposal for the correct field; never move '
            'text between fields. For non_unique anchors, extend the anchor with adjacent '
            'unchanged text until it occurs exactly once, or leave the operation unresolved. '
            'Return the full intended operations, not an empty no-op to evade validation.'
            '\nWrap the complete corrected operations object in {"action":{"operations":...}}. '
            'Never output Python or Markdown fences.')

    def render_resolution_repair(self, issues):
        return super().render_resolution_repair(issues) + (
            '\nWrap the corrected operations object in {"action":{"operations":...}}.')
