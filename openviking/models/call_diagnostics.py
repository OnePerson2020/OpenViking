"""Opt-in, metadata-only model-call diagnostics. Never persist prompts or headers."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_scope: ContextVar[dict[str, Any]] = ContextVar('model_diagnostic_scope', default={})
_call: ContextVar['CallObservation | None'] = ContextVar('model_call_observation', default=None)
_lock = threading.Lock()
_sinks: dict[str, logging.Logger] = {}


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding,
                    opener=lambda path, flags: os.open(path, flags, 0o600))


def _identifier(value: Any) -> str | None:
    if value is None:
        return None
    # IDs and enum labels only. In particular, never log arbitrary exception messages.
    return re.sub(r'[^a-zA-Z0-9_.:/,@+-]', '_', str(value))[:256]


def _emit(record: dict[str, Any], file_name: str | None = None) -> None:
    try:
        from openviking_cli.utils.config import get_openviking_config
        path = getattr(get_openviking_config().log, 'model_calls_output', '')
        if not path:
            return
        if file_name:
            path = str(Path(path).expanduser().with_name(file_name))
        with _lock:
            sink = _sinks.get(path)
            if sink is None:
                target = Path(path).expanduser()
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.touch(mode=0o600, exist_ok=True)
                target.chmod(0o600)
                handler = _PrivateRotatingHandler(target, maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8', delay=True)
                handler.setFormatter(logging.Formatter('%(message)s'))
                sink = logging.Logger('openviking.model_calls.private', level=logging.INFO)
                sink.propagate = False
                sink.addHandler(handler)
                _sinks[path] = sink
        sink.info(json.dumps({'time_unix': time.time(), **record}, ensure_ascii=True))
    except Exception:
        # Optional diagnostics must never change model or storage success semantics.
        pass


@contextmanager
def model_call_scope(**values: Any):
    allowed = {'session_id', 'archive_uri', 'batch', 'batch_count', 'batch_attempt',
               'checkpoint_count', 'conversation_estimated_tokens', 'prior_wm_estimated_tokens'}
    scope = dict(_scope.get())
    for key, value in values.items():
        if key in allowed:
            scope[key] = value if isinstance(value, (int, float)) else _identifier(value)
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


class CallObservation:
    def __init__(self, *, model: str, input_tokens: int, max_tokens: int | None,
                 tool_names: list[str], thinking: bool, timeout: float):
        self.started = time.monotonic()
        self.fields = {
            **_scope.get(), 'call_id': uuid.uuid4().hex,
            'provider': 'volcengine', 'model': _identifier(model),
            'input_estimated_tokens': input_tokens, 'max_output_tokens': max_tokens,
            'tools': [_identifier(name) for name in tool_names],
            'thinking': thinking, 'stream': False, 'timeout_s': timeout,
        }
        try:
            from openviking.service.task_work_index import get_task_context
            context = get_task_context()
            if context is not None:
                self.fields['task_id'] = _identifier(context.task_id)
        except Exception:
            pass
        self.transport: dict[str, Any] = {}
        self.attempt = 0
        self.token = None

    def __enter__(self):
        self.token = _call.set(self)
        _emit({**self.fields, 'event': 'call_started'})
        return self

    def begin_attempt(self, attempt: int) -> None:
        self.attempt = attempt
        self.transport = {}

    def snapshot(self) -> dict[str, Any]:
        return {**self.fields, **self.transport, 'attempt': self.attempt,
                'elapsed_s': round(time.monotonic() - self.started, 3)}

    def complete(self, response: Any) -> None:
        usage = getattr(response, 'usage', None)
        choices = getattr(response, 'choices', None) or []
        fields = {
            **self.snapshot(), 'event': 'call_completed',
            'response_id': _identifier(getattr(response, 'id', None)),
            'finish_reason': _identifier(getattr(choices[0], 'finish_reason', None)) if choices else None,
            'prompt_tokens': getattr(usage, 'prompt_tokens', None),
            'completion_tokens': getattr(usage, 'completion_tokens', None),
            # Provider prefix-cache hits (Ark: usage.prompt_tokens_details.cached_tokens).
            'cached_tokens': getattr(getattr(usage, 'prompt_tokens_details', None), 'cached_tokens', None),
        }
        headers_at = self.transport.get('headers_elapsed_s')
        if headers_at is not None:
            fields['after_headers_s'] = round(fields['elapsed_s'] - headers_at, 3)
        _emit(fields)
        if fields['finish_reason'] == 'length' and choices:
            # Local-only sample of runaway outputs (2026-10-09 diagnosis); never the prompt.
            text = str(getattr(getattr(choices[0], 'message', None), 'content', None) or '')
            _emit({'call_id': fields['call_id'], 'task_id': fields.get('task_id'),
                   'completion_tokens': fields['completion_tokens'], 'chars': len(text),
                   'head': text[:500], 'tail': text[-2000:]}, 'truncated-outputs.jsonl')

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            _emit({**self.snapshot(), 'event': 'call_failed', 'error_type': exc_type.__name__})
        _call.reset(self.token)


async def request_hook(request) -> None:
    observation = _call.get()
    if observation is None:
        return
    observation.transport['http_request_started_s'] = round(time.monotonic() - observation.started, 3)
    observation.transport['transport_phase'] = 'request_started'
    client_id = request.headers.get('x-client-request-id', '')
    if re.fullmatch(r'ToB-direct,OpenViking_Service,openviking-service_cn-beijing,[0-9a-f]{32}', client_id):
        observation.transport['client_request_id'] = client_id
    async def trace(name, info):
        # httpcore's info can contain request bodies, sockets and exceptions.
        # Retain ONLY event name and elapsed time, not the info payload.
        observation.transport['transport_phase'] = _identifier(name)
        observation.transport['transport_phase_elapsed_s'] = round(time.monotonic() - observation.started, 3)
    request.extensions.setdefault('trace', trace)


async def response_hook(response) -> None:
    observation = _call.get()
    if observation is None:
        return
    observation.transport.update(
        http_status=response.status_code,
        headers_elapsed_s=round(time.monotonic() - observation.started, 3),
    )
    # Allowlist provider correlation identifiers. Never dump all response headers.
    for name in ('x-request-id', 'x-tt-logid', 'x-ark-request-id'):
        if response.headers.get(name):
            observation.transport[name.replace('-', '_')] = _identifier(response.headers[name])


def attach_hooks(client) -> None:
    client.event_hooks.setdefault('request', []).append(request_hook)
    client.event_hooks.setdefault('response', []).append(response_hook)
