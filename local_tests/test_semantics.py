import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from jsonschema import ValidationError
from openviking.models.vlm.base import VLMResponse
from openviking.session.memory.extraction_output_protocol import ExtractionOutputContext
from openviking.session.memory.extraction_output_protocol.strict_json_protocol import StrictJsonExtractionOutputProtocol, StrictActionError
from openviking.session.memory.schema_model_generator import SchemaModelGenerator
from openviking.session.memory.memory_type_registry import get_default_registry
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.context_budget import validate_partial_fields
from protocol_fixtures import _context, _preference_schema, _existing_preference


class SemanticTests(TestCase):
    def setUp(self):
        self.protocol=StrictJsonExtractionOutputProtocol()

    def test_original_experience_nullable_supersedes_accepted(self):
        schema=get_default_registry().get('experiences');ctx=_context([schema])
        raw={'action':{'operations':{'experiences':[{'page_id':100,'experience_name':'test','content':'full body','supersedes':None}],'delete_ids':[]}}}
        _,ops=self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])
        self.assertIsNone(ops.experiences[0].supersedes)

    def test_missing_field_not_repaired(self):
        schema=get_default_registry().get('experiences');ctx=_context([schema])
        raw={'action':{'operations':{'experiences':[{'page_id':100,'experience_name':'test','content':'full body'}],'delete_ids':[]}}}
        with self.assertRaises(ValidationError):self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])

    def delete_context(self, resolver, metadata=None):
        uri='viking://user/a/memories/preferences/editor.md';mf=_existing_preference(uri,'editor','body');mf.memory_type=metadata
        ctx=_context([_preference_schema()],files=[mf]);ctx.memory_type_resolver=resolver
        raw={'action':{'operations':{'preferences':[],'delete_ids':[{'delete_page_id':1,'replacement_page_id':None}]}}}
        return ctx,raw

    def test_missing_metadata_uses_trusted_unique_resolver(self):
        ctx,raw=self.delete_context(lambda uri:'preferences')
        _,ops=self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])
        self.assertEqual(len(ops.delete_ids),1)

    def test_ambiguous_path_not_allowed(self):
        # 2026-10-06: an undeletable target is dropped (not deleting is safe), not fatal.
        ctx,raw=self.delete_context(lambda uri:None)
        with self.assertLogs('openviking.session.memory.extraction_output_protocol.strict_json_protocol',level='WARNING') as cm:
            _,ops=self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])
        self.assertEqual(len(ops.delete_ids),0);self.assertIn('DELETE_TYPE_NOT_ALLOWED',cm.output[0])

    def test_metadata_path_conflict_rejected(self):
        ctx,raw=self.delete_context(lambda uri:'preferences','events')
        with self.assertLogs('openviking.session.memory.extraction_output_protocol.strict_json_protocol',level='WARNING') as cm:
            _,ops=self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])
        self.assertEqual(len(ops.delete_ids),0);self.assertIn('DELETE_TYPE_CONFLICT',cm.output[0])

    def test_error_does_not_expose_business_body(self):
        ctx=_context([_preference_schema()]);raw={'action':{'operations':{'preferences':[{'page_id':100,'topic':'private topic','content':{'blocks':[{'search':'secret','replace':'secret2'}]},'score':0}],'delete_ids':[]}}}
        with self.assertRaises(StrictActionError) as cm:self.protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,self.protocol.response_format(ctx,[]),[])
        self.assertIn('NEW_VALUE_IS_PATCH',str(cm.exception));self.assertIn('preferences.content',str(cm.exception));self.assertNotIn('secret',str(cm.exception));self.assertNotIn('private',str(cm.exception))


class ResolveTests(IsolatedAsyncioTestCase):
    async def test_null_keep_survives_pydantic_resolve_and_partial_guard(self):
        uri='viking://user/a/memories/preferences/editor.md';mf=_existing_preference(uri,'editor','visible hidden body',4)
        schema=_preference_schema();ctx=_context([schema],files=[mf]);protocol=StrictJsonExtractionOutputProtocol()
        raw={'action':{'operations':{'preferences':[{'page_id':1,'topic':'editor','content':None,'score':None}],'delete_ids':[]}}}
        _,model=protocol.parse_response(VLMResponse(content=json.dumps(raw)),ctx,protocol.response_format(ctx,[]),[])
        isolation=Mock();isolation.get_read_scope.return_value=None;isolation.fill_identity_fields.side_effect=lambda item,**kw:item;isolation.render_schema_directories.return_value=['viking://user/a/memories/preferences']
        provider=SimpleNamespace(get_memory_schemas=lambda c:[schema],read_file_contents={uri:mf},partial_read_fields={uri:{'content':['visible']}})
        loop=ExtractLoop(SimpleNamespace(model='fake'),viking_fs=Mock(),context_provider=provider,isolation_handler=isolation)
        loop._output_protocol=protocol;loop._extract_context=SimpleNamespace(page_id_map=ctx.page_id_map)
        resolved,_=await loop.resolve_operations(model)
        self.assertNotIn('content',resolved.upsert_operations[0].memory_fields)
        self.assertNotIn('score',resolved.upsert_operations[0].memory_fields)
        self.assertEqual(loop._partial_operation_errors(resolved),[])
        self.assertEqual(mf.content,'visible hidden body')

    async def test_visible_full_replacement_is_still_rejected(self):
        uri='viking://user/a/memories/preferences/editor.md';mf=_existing_preference(uri,'editor','visible hidden')
        with self.assertRaises(ValueError):validate_partial_fields(mf,{'content':'wipe'}, {'content':['visible']})

    async def test_specific_error_reaches_retry_instruction(self):
        from test_strict_extraction import LoopTests
        loop,vlm=LoopTests().make_loop(VLMResponse(content='{"action":{"operations":{"preferences":[{"page_id":5,"topic":"x","content":"body","score":0}],"delete_ids":[]}}}'))
        with self.assertLogs('openviking.session.memory.extract_loop',level='WARNING'):
            self.assertEqual(await loop._call_llm([]),(None,None))
        self.assertIn('NEW_PAGE_ID_RANGE',loop._last_parse_error)
        msg=[];loop._add_format_error_message(msg)
        self.assertIn('NEW_PAGE_ID_RANGE',msg[0]['content'])
