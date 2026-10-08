import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase,TestCase
from unittest.mock import patch
from openviking.session.memory.extraction_output_protocol.strict_json_protocol import StrictJsonExtractionOutputProtocol
from protocol_fixtures import _context,_preference_schema
from openviking.session.memory.memory_type_registry import get_default_registry
from openviking.session.memory.dataclass import MemoryFile

class FieldTests(TestCase):
    def test_multifield_display_body_not_exposed_as_writable_field(self):
        s=get_default_registry().get('soul');ctx=_context([s]);ctx.memory_type_resolver=lambda uri:'soul';ctx.page_id_map.get_page_id('viking://user/a/memories/soul.md')
        p=StrictJsonExtractionOutputProtocol();data={'page_id':1,'content':'combined body','core_truths':'exact truth','continuity':'exact continuity','_partial':True,'_omitted_fields':['core_truths']}
        original=dict(data);r=p._field_scoped_result(data,ctx)
        self.assertNotIn('content',r);self.assertEqual(r['core_truths'],'exact truth');self.assertEqual(r['_omitted_fields'],['core_truths']);self.assertEqual(data,original)

    def test_real_content_field_is_not_removed(self):
        ctx=_context([_preference_schema()]);ctx.memory_type_resolver=lambda uri:'preferences';ctx.page_id_map.get_page_id('uri')
        d={'page_id':1,'content':'body','topic':'editor'};self.assertEqual(StrictJsonExtractionOutputProtocol()._field_scoped_result(d,ctx),d)

    def test_prefetch_and_tool_reads_share_field_scope(self):
        s=get_default_registry().get('soul');ctx=_context([s]);ctx.memory_type_resolver=lambda uri:'soul';ctx.page_id_map.get_page_id('uri');p=StrictJsonExtractionOutputProtocol();result={'page_id':1,'content':'rendering','continuity':'exact'}
        raw={'role':'user','content':json.dumps({'tool_call_name':'read','args':{'uri':'uri'},'result':result})}
        msg=p.render_prefetch_messages([raw],ctx)[0];self.assertNotIn('content',json.loads(msg['content'])['result'])
