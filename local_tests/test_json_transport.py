import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from openviking.session.session import Session, WM_UPDATE_TOOL, WM_SEVEN_SECTIONS
from openviking.models.vlm.base import VLMResponse, ToolCall
from openviking.models.vlm.backends.volcengine_vlm import VolcEngineVLM
from openviking_cli.utils.config.vlm_config import VLMConfig
from openviking_cli.utils.config.memory_config import MemoryConfig


def payload():return {'sections':{name:{'op':'KEEP'} for name in WM_SEVEN_SECTIONS}}

class JsonTransport(unittest.IsolatedAsyncioTestCase):
    async def invoke(self,responses,budget=128000):
        network=AsyncMock(side_effect=responses)
        cfg=SimpleNamespace(memory=SimpleNamespace(extraction_input_token_budget=budget,working_memory_transport='json_schema'),vlm=SimpleNamespace(get_completion_async=network))
        with patch('openviking.session.session.get_openviking_config',return_value=cfg):
            result=await Session._complete_wm_tool_request(object.__new__(Session),'source',WM_UPDATE_TOOL,{'type':'function'})
        return result,network

    async def test_success_preserves_arguments_and_usage_without_tools(self):
        response=VLMResponse(content=json.dumps(payload()),finish_reason='stop',usage={'prompt_tokens':42})
        result,network=await self.invoke([response]);self.assertEqual(result.tool_calls[0].arguments,payload());self.assertEqual(result.usage,response.usage)
        self.assertNotIn('tools',network.await_args.kwargs)
        fmt=network.await_args.kwargs['response_format'];self.assertTrue(fmt['json_schema']['strict']);self.assertEqual(fmt['json_schema']['schema'],WM_UPDATE_TOOL['function']['parameters'])

    async def test_invalid_json_gets_one_retry_then_fails(self):
        with self.assertRaisesRegex(ValueError,'after 2 attempts'):
            await self.invoke([VLMResponse(content='{}'),VLMResponse(content='{}')])

    async def test_length_refusal_wrong_wrapper_rejected(self):
        for bad in [VLMResponse(content=json.dumps(payload()),finish_reason='length'),VLMResponse(content=None),VLMResponse(content=json.dumps(payload()),tool_calls=[ToolCall('id','wrong',{})]),'plain string without metadata']:
            good=VLMResponse(content=json.dumps(payload()))
            result,network=await self.invoke([bad,good]);self.assertTrue(result.has_tool_calls);self.assertEqual(network.await_count,2)

    async def test_transport_cancellation_and_budget_not_swallowed(self):
        for error in [ConnectionError('offline'),asyncio.CancelledError()]:
            with self.assertRaises(type(error)):await self.invoke([error])
        with self.assertRaisesRegex(ValueError,'budget exceeded'):await self.invoke([],budget=1)

    async def test_schema_tokens_included_in_guard(self):
        fmt={'type':'json_schema','json_schema':{'schema':{'description':'large '*10000}}}
        self.assertGreater(Session._working_memory_request_tokens('source',response_format=fmt),Session._working_memory_request_tokens('source')+10000)

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
        await client.aclose();self.assertIsInstance(result,VLMResponse);self.assertEqual(result.finish_reason,'length');self.assertEqual(seen[0]['response_format'],fmt);self.assertIsNone(seen[0].get('tools'))
        with self.assertRaises(ValueError):await backend.get_completion_async('source',response_format=fmt,tools=[WM_UPDATE_TOOL])

    def test_default_legacy_explicit_config_only(self):
        self.assertEqual(MemoryConfig().working_memory_transport,'legacy')
        self.assertEqual(MemoryConfig(working_memory_transport='json_schema').working_memory_transport,'json_schema')
        with self.assertRaises(ValueError):MemoryConfig(working_memory_transport='bogus')
