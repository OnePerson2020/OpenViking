"""response_format passthrough to the VLM backend (WM transport tests dropped with WM, v0.5.0)."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from openviking.models.vlm.base import VLMResponse
from openviking.models.vlm.backends.volcengine_vlm import VolcEngineVLM
from openviking_cli.utils.config.vlm_config import VLMConfig

TOOL = {'type': 'function', 'function': {'name': 't', 'parameters': {'type': 'object'}}}


class JsonTransport(unittest.IsolatedAsyncioTestCase):
    async def test_config_only_forwards_to_supported_backend(self):
        config=VLMConfig(provider='volcengine',model='test',api_key='test')
        backend=SimpleNamespace(supports_structured_output=True,get_completion_async=AsyncMock(return_value='sentinel'))
        config._vlm_instance=backend
        await config.get_completion_async('source',response_format={'type':'json_schema'})
        self.assertIn('response_format',backend.get_completion_async.await_args.kwargs)
        backend.supports_structured_output=False
        with self.assertRaisesRegex(ValueError,'does not expose'):await config.get_completion_async('source',response_format={})
        await config.get_completion_async('legacy')
        self.assertNotIn('response_format',backend.get_completion_async.await_args.kwargs)

    async def test_actual_sdk_transmits_schema_and_preserves_length(self):
        seen=[]
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200,json={'id':'id','object':'chat.completion','created':1,'model':'test','choices':[{'index':0,'finish_reason':'length','message':{'role':'assistant','content':'{}'}}],'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
        from volcenginesdkarkruntime import AsyncArk
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk=AsyncArk(base_url='http://test',api_key='test',http_client=client,max_retries=0)
        backend=VolcEngineVLM({'model':'test','api_key':'test','timeout':1,'max_retries':0});backend.get_async_client=lambda:sdk
        fmt={'type':'json_schema','json_schema':{'name':'test','strict':True,'schema':{'type':'object'}}}
        result=await backend.get_completion_async('source',response_format=fmt)
        await backend.get_completion_async('plain text');self.assertIsNone(seen[1].get('stop'))  # text calls keep blank lines
        await client.aclose();self.assertIsInstance(result,VLMResponse);self.assertEqual(result.finish_reason,'length');self.assertEqual(seen[0]['response_format'],fmt);self.assertIsNone(seen[0].get('tools'))
        self.assertEqual(seen[0]['stop'],['\n\n',' \n'])  # runaway whitespace ends the call
        with self.assertRaises(ValueError):await backend.get_completion_async('source',response_format=fmt,tools=[TOOL])
