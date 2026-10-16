"""The VLM call retries only transient errors and stops at one wall-clock deadline."""
import asyncio
import time
import unittest

import httpx
from volcenginesdkarkruntime import AsyncArk

from openviking.models.vlm.backends.volcengine_vlm import VolcEngineVLM
from openviking.utils.exceptions import ModelCallDeadlineError

OK = {'id': 'id', 'object': 'chat.completion', 'created': 1, 'model': 'test',
      'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
      'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


class VlmRetry(unittest.IsolatedAsyncioTestCase):
    async def call(self, statuses, timeout=30, delay=0.0, vision=False, failed=None):
        calls = []

        async def handler(request):
            calls.append(1)
            await asyncio.sleep(delay)
            status = statuses[min(len(calls), len(statuses)) - 1]
            body = OK if status == 200 else {'error': {'code': 'ServerOverloaded' if status == 429 else 'InvalidParameter', 'message': 'bad'}}
            return httpx.Response(status, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk = AsyncArk(base_url='http://test', api_key='test', http_client=client, max_retries=0)
        backend = VolcEngineVLM({'model': 'test', 'api_key': 'test', 'timeout': timeout, 'max_retries': 1})
        backend.get_async_client = lambda: sdk
        if failed is not None:
            backend.record_failed_call = lambda **kw: failed.append(kw['error'])
        complete = backend.get_vision_completion_async if vision else backend.get_completion_async
        try:
            return await complete('hi'), calls
        except Exception as exc:
            return exc, calls
        finally:
            await client.aclose()

    async def test_permanent_error_is_not_retried(self):
        result, calls = await self.call([400])
        self.assertIsInstance(result, Exception)
        self.assertEqual(len(calls), 1)

    async def test_transient_error_is_retried(self):
        result, calls = await self.call([503, 200])
        self.assertEqual(result, 'ok')
        self.assertEqual(len(calls), 2)

    async def test_deadline_bounds_all_attempts(self):
        started = time.monotonic()
        result, calls = await self.call([503, 503], timeout=0.3, delay=0.2)
        self.assertIsInstance(result, ModelCallDeadlineError)
        self.assertLess(time.monotonic() - started, 1.0)

    async def test_deadline_counts_the_cancelled_attempt_as_failed(self):
        failed = []
        result, calls = await self.call([200], timeout=0.2, delay=1.0, failed=failed)
        self.assertIsInstance(result, ModelCallDeadlineError)
        self.assertEqual(len(failed), 1)

    async def test_vision_overload_is_retried(self):
        result, calls = await self.call([429, 200], vision=True)
        self.assertEqual(result, 'ok')
        self.assertEqual(len(calls), 2)

    async def test_vision_permanent_error_is_not_retried(self):
        result, calls = await self.call([400], vision=True)
        self.assertIsInstance(result, Exception)
        self.assertEqual(len(calls), 1)

    async def test_vision_deadline_bounds_all_attempts(self):
        result, calls = await self.call([503, 503], timeout=0.3, delay=0.2, vision=True)
        self.assertIsInstance(result, ModelCallDeadlineError)
