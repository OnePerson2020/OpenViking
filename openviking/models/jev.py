# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Generic asynchronous client for Jev-compatible System One decisions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx

from openviking.telemetry import tracer
from openviking_cli.utils.config.jev_config import JevConfig


class JevError(RuntimeError):
    """Raised when a System One request or response is invalid."""


@dataclass(slots=True)
class JevClient:
    """Evaluate typed questions against shared state through System One."""

    config: JevConfig
    client: Any = None

    @tracer("model.jev.evaluate", ignore_args=True, ignore_result=True)
    async def evaluate(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not questions:
            return {}
        tracer.set("jev.question_count", len(questions))
        tracer.set("jev.state_chars", len(json.dumps(state, ensure_ascii=False, default=str)))
        payload: dict[str, Any] = {"state": state, "questions": questions}
        if self.config.model:
            payload["model"] = self.config.model

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        try:
            body = await self._post_with_retry(client, payload)
        finally:
            if owns_client:
                await client.aclose()

        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            raise JevError("Jev decision response has no answers object")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        for key in ("input_tokens", "state_tokens", "question_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                tracer.set(f"jev.{key}", value)
        return answers

    async def _post_with_retry(self, client: Any, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.post(self.config.api_url, json=payload)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise JevError("Jev decision response must be a JSON object")
                return body
            except Exception as exc:
                if attempt >= self.config.max_retries or not _retryable_jev_error(exc):
                    raise JevError(f"Jev decision request failed: {exc}") from exc
                await asyncio.sleep(_retry_delay(self.config, attempt=attempt, error=exc))
        raise AssertionError("unreachable")


def _retryable_jev_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504, 529}
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _retry_delay(config: JevConfig, *, attempt: int, error: Exception) -> float:
    delay = config.retry_backoff_seconds * (2**attempt)
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
    return min(delay, config.timeout)
