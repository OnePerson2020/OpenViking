# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Provider-constrained JSON actions; no Python and no JSON text repair.

Two deterministic, meaning-preserving adjustments are applied to final
operations: new-page ids the provider cannot constrain (``minimum``) are
renumbered, and deletions of types that can never be deleted are dropped.
When retries are exhausted, ``salvage`` keeps the valid items of the last
response and drops only the individually invalid ones.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from openviking.models.vlm.base import ToolCall, VLMResponse
from openviking.session.memory.extraction_output_protocol.json_protocol import JsonExtractionOutputProtocol
from openviking_cli.utils import get_logger

logger = get_logger(__name__)


class StrictActionError(ValueError):
    """Safe, actionable diagnostics: codes/known schema fields, never response text."""
    def __init__(self, code, message, path="", item=None):
        self.code = code
        self.path = path
        # (operations key, list index) of the single offending item, when the
        # error is confined to one item and dropping it is a valid salvage.
        self.item = item
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


def _strict_decoder() -> json.JSONDecoder:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise StrictActionError('JSON_DUPLICATE_KEY', 'Each JSON object key must occur once')
            result[key] = value
        return result
    def constant(_):
        raise StrictActionError('JSON_NONFINITE', 'JSON numbers must be finite')
    return json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)


def strict_loads(content: str) -> Any:
    return _strict_decoder().decode(content)


_OPERATIONS_PREFIX = re.compile(r'\s*\{\s*"action"\s*:\s*\{\s*"operations"\s*:\s*\{')


def truncated_operations(content: Any) -> dict[str, list] | None:
    """Complete list items of `action.operations` in a cut-off response, else None.

    Walks `{"action":{"operations":{"<type>":[item, ...], ...` and keeps every
    item that decodes completely; stops at the first incomplete or malformed
    token. Items are returned unvalidated: ``salvage`` checks each of them.
    """
    if not isinstance(content, str):
        return None
    match = _OPERATIONS_PREFIX.match(content)
    if not match:
        return None
    decoder = _strict_decoder()
    def decode(pos):
        try:
            return decoder.raw_decode(content, pos)
        except ValueError:  # includes StrictActionError
            return None, pos
    def skip(pos):
        while pos < len(content) and content[pos] in ' \t\n\r':
            pos += 1
        return pos
    ops, pos = {}, match.end()
    while True:
        pos = skip(pos)
        key, pos = decode(pos)
        pos = skip(pos)
        if not isinstance(key, str) or key in ops or content[pos:pos + 1] != ':':
            break
        pos = skip(pos + 1)
        if content[pos:pos + 1] != '[':
            break
        items, pos = [], pos + 1
        ops[key] = items
        while True:
            pos = skip(pos)
            if content[pos:pos + 1] == ']':
                pos += 1
                break
            item, end = decode(pos)
            if end == pos:
                return {k: v for k, v in ops.items() if v} or None
            items.append(item)
            pos = skip(end)
            if content[pos:pos + 1] == ',':
                pos += 1
        pos = skip(pos)
        if content[pos:pos + 1] != ',':
            break
        pos += 1
    return {k: v for k, v in ops.items() if v} or None



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
        self._last_operations = None
        if not isinstance(response, VLMResponse):
            raise StrictActionError('RESPONSE_METADATA_MISSING', 'Structured response metadata is required')
        if response.finish_reason != 'stop' or response.has_tool_calls:
            if response.finish_reason == 'length' and not response.has_tool_calls:
                # Runaway generations (whitespace / repeated-key loops) hit the cap
                # after complete items; only the terminal salvage may use those.
                ops = truncated_operations(response.content)
                if ops:
                    self.normalize_new_page_ids(ops, context)
                    self._last_operations = ops
            raise StrictActionError('RESPONSE_NOT_COMPLETE', 'Return complete JSON with stop; native tool wrappers are not accepted')
        if not isinstance(response.content, str):
            raise StrictActionError('RESPONSE_CONTENT_MISSING', 'JSON string content is required')
        raw = strict_loads(response.content)
        pending = raw.get('action') if isinstance(raw, dict) else None
        if isinstance(pending, dict) and isinstance(pending.get('operations'), dict):
            # Before schema validation: providers do not enforce `minimum`, so a
            # new event numbered 0/1 would otherwise fail the whole response.
            self.normalize_new_page_ids(pending['operations'], context)
            self._last_operations = deepcopy(pending['operations'])
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
        dropped = []
        self.validate_business_shape(operations, context, dropped)
        if dropped:
            logger.warning("Strict extraction dropped unsafe deletion(s): %s", dropped)
        model = context.operations_model.model_validate_json(json.dumps(operations), strict=True)
        return None, model

    @staticmethod
    def normalize_new_page_ids(operations, context):
        """Renumber add-only (event) page ids into the unused >=100 range.

        An add-only item whose id names no existing page is a new page, so the
        id carries no reference: one below 100 or one repeated by another new
        item is renumbered. Ids of existing pages and all mutable types are left
        for the business checks, since there the id may mean a real target.
        """
        add_only = [s.memory_type for s in context.schemas if s.operation_mode == 'add_only']
        if not add_only:
            return
        resolve = context.page_id_map.resolve
        used = {item.get('page_id') for s in context.schemas for item in operations.get(s.memory_type) or []
                if isinstance(item, dict)}
        ints = [pid for pid in used if isinstance(pid, int) and not isinstance(pid, bool)]
        next_id = max([99, *ints]) + 1
        # New ids already claimed by mutable-type items win over event ids.
        new_seen = {item.get('page_id') for s in context.schemas if s.memory_type not in add_only
                    for item in operations.get(s.memory_type) or [] if isinstance(item, dict)}
        for name in add_only:
            for item in operations.get(name) or []:
                pid = item.get('page_id') if isinstance(item, dict) else None
                if not isinstance(pid, int) or isinstance(pid, bool):
                    continue
                if resolve(pid):
                    continue
                if pid < 100 or pid in new_seen:
                    while next_id in used or resolve(next_id):
                        next_id += 1
                    used.add(next_id)
                    item['page_id'] = next_id
                new_seen.add(item['page_id'])

    @staticmethod
    def validate_business_shape(operations, context, dropped=None):
        """Preserve the Python protocol's complete-create and safe-delete boundary.

        A deletion of a type that may never be deleted is dropped (recorded in
        ``dropped``) rather than failing the response: not deleting is the safe
        outcome. Item-level errors carry ``item`` so ``salvage`` can drop them.
        """
        seen = set()
        singleton_targets = set()
        by_type = {s.memory_type: s for s in context.schemas}
        for name, schema in by_type.items():
            for index, item in enumerate(operations.get(name, [])):
                at = (name, index)
                page_id = item['page_id']
                if page_id in seen:
                    raise StrictActionError('DUPLICATE_PAGE_ID', 'Combine changes for an existing page or assign distinct new page IDs', name, at)
                seen.add(page_id)
                uri = context.page_id_map.resolve(page_id)
                if schema.operation_mode == 'add_only' and uri:
                    raise StrictActionError('ADD_ONLY_EXISTING_PAGE', 'Use a new unregistered page_id >=100 for add-only memories', name, at)
                if not uri:
                    if page_id < 100:
                        raise StrictActionError('NEW_PAGE_ID_RANGE', 'New memory page_id must be at least 100', name, at)
                    for field in schema.fields:
                        # Presence is required. Nullability is decided by the
                        # already-validated original schema, just as in the Python
                        # compiler (e.g. supersedes=null means no replaced experience).
                        if field.name not in item:
                            raise StrictActionError('NEW_FIELD_MISSING', 'Supply every new-memory field; nullable fields may be null', name + '.' + field.name, at)
                        if isinstance(item[field.name], dict) and 'blocks' in item[field.name]:
                            raise StrictActionError('NEW_VALUE_IS_PATCH', 'New memories need complete values, not patch blocks', name + '.' + field.name, at)
                if not schema.filename_has_variables():
                    target = (name, item.get('peer_id'))
                    if target in singleton_targets:
                        raise StrictActionError('DUPLICATE_SINGLETON', 'Combine changes into one operation per singleton target', name, at)
                    singleton_targets.add(target)
        deletions = operations.get('delete_ids', [])
        for index in range(len(deletions) - 1, -1, -1):
            deletion = deletions[index]
            at = ('delete_ids', index)
            uri = context.page_id_map.resolve(deletion['delete_page_id'])
            memory = context.read_file_contents.get(uri)
            if memory is None:
                raise StrictActionError('DELETE_UNREAD', 'Read the existing target before proposing its deletion', 'delete_ids', at)
            resolver = context.memory_type_resolver
            memory_type = resolver(uri) if callable(resolver) else memory.memory_type
            schema = by_type.get(memory_type)
            conflict = callable(resolver) and memory.memory_type and memory_type != memory.memory_type
            if conflict or schema is None or schema.operation_mode == 'add_only':
                if dropped is None:
                    code = 'DELETE_TYPE_CONFLICT' if conflict else 'DELETE_TYPE_NOT_ALLOWED'
                    raise StrictActionError(code, 'Only an unambiguously identified, deletable allowed type may be deleted', 'delete_ids', at)
                dropped.append(f"delete_ids[{index}]:{'DELETE_TYPE_CONFLICT' if conflict else 'DELETE_TYPE_NOT_ALLOWED'}")
                del deletions[index]
                continue
            if deletion['delete_page_id'] in seen:
                raise StrictActionError('DELETE_UPDATE_CONFLICT', 'Do not update and delete the same page', 'delete_ids', at)
            replacement = deletion.get('replacement_page_id')
            if replacement is not None and replacement not in seen and not context.page_id_map.resolve(replacement):
                raise StrictActionError('DELETE_REPLACEMENT_UNKNOWN', 'Reference an existing or newly declared replacement page', 'delete_ids', at)

    def salvage(self, context):
        """Last resort after retries: keep the valid items of the last final response.

        Drops only items whose own schema or business check fails; any error
        that is not confined to one item, or a result with nothing left, returns
        ``(None, dropped)`` so the failure stays visible.
        """
        operations = deepcopy(getattr(self, '_last_operations', None))
        dropped = []
        if not isinstance(operations, dict):
            return None, dropped
        original = sum(len(v) for v in operations.values() if isinstance(v, list))
        schema = context.operations_model.model_json_schema()
        allowed = set(schema.get('properties', {}))
        for key in [k for k in operations if allowed and k not in allowed]:
            dropped.append(f"{key}:unknown")
            del operations[key]
        validator = Draft202012Validator(schema)
        for _ in range(500):
            error = next(iter(validator.iter_errors(operations)), None)
            if error is not None:
                path = list(error.absolute_path)
                if (len(path) >= 2 and isinstance(path[1], int)
                        and isinstance(operations.get(path[0]), list) and path[1] < len(operations[path[0]])):
                    dropped.append(f"{path[0]}[{path[1]}]:{error.validator}")
                    del operations[path[0]][path[1]]
                    continue
                return None, dropped
            try:
                self.validate_business_shape(operations, context, dropped)
            except StrictActionError as exc:
                if exc.item is None:
                    return None, dropped
                name, index = exc.item
                dropped.append(f"{name}[{index}]:{exc.code}")
                del operations[name][index]
                continue
            kept = sum(len(v) for v in operations.values() if isinstance(v, list))
            if original and not kept:
                return None, dropped
            try:
                model = context.operations_model.model_validate_json(json.dumps(operations), strict=True)
            except ValueError:
                return None, dropped
            return model, dropped
        return None, dropped

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
