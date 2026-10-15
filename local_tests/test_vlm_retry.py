"""The VLM deadline loop retries only transient errors (upstream retry classification)."""
import unittest

import httpx
from volcenginesdkarkruntime import AsyncArk

from openviking.models.vlm.backends.volcengine_vlm import VolcEngineVLM

OK = {'id': 'id', 'object': 'chat.completion', 'created': 1, 'model': 'test',
      'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
      'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


class VlmRetry(unittest.IsolatedAsyncioTestCase):
    async def call(self, statuses):
        calls = []

        def handler(request):
            calls.append(1)
            status = statuses[min(len(calls), len(statuses)) - 1]
            body = OK if status == 200 else {'error': {'code': 'InvalidParameter', 'message': 'bad'}}
            return httpx.Response(status, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk = AsyncArk(base_url='http://test', api_key='test', http_client=client, max_retries=0)
        backend = VolcEngineVLM({'model': 'test', 'api_key': 'test', 'timeout': 30, 'max_retries': 1})
        backend.get_async_client = lambda: sdk
        try:
            return await backend.get_completion_async('hi'), calls
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
