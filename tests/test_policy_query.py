import json
import sqlite3
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from powercopy.data.query import PolicyQueryService, PolicyQueryError
from powercopy.tools.builtin.policy_db import build_policy_bindings
from test_policy_versioning import raw, PROFILE
from powercopy.data.parser import ParseProfile
from powercopy.data.versioning import VersionedPolicyStore

@pytest.fixture
def sample(tmp_path):
    store=VersionedPolicyStore(tmp_path/'政策库.db')
    a=store.ingest(raw(text='第一条 电力交易旧规则',title='电力市场'),PROFILE)
    b=store.ingest(raw('f2','第一条 电力交易新规则',1,title='电力市场'),PROFILE)
    store.ingest(raw('f3','第一条 电力交易新规则',2,title='电力市场'),PROFILE)
    anhui=store.ingest(raw('f4','安徽电力交易 100% _',url='https://example.invalid/a',title='安徽市场'),ParseProfile(title_selector='h1',body_selector='article',region='安徽'))
    jiangsu=store.ingest(raw('f5','江苏电力交易',url='https://example.invalid/b',title='江苏市场'),ParseProfile(title_selector='h1',body_selector='article',region='江苏'))
    return store,PolicyQueryService(store.db_path),a,b,anhui,jiangsu

def test_latest_only(sample):
    _,q,a,b,*_=sample
    results=q.search_policy('','电力')['items']
    assert len(results)==3
    assert next(x for x in results if x['policy_id']==a.policy_id)['document_id']==b.document_id
    assert not q.search_policy('','旧规则')['items']
    assert all('body_text' not in x for x in results)

def test_region(sample):
    _,q,a,b,anhui,jiangsu=sample
    items=q.search_policy('安徽','交易')['items']
    assert {x['policy_id'] for x in items}=={a.policy_id,anhui.policy_id}
    assert {x['region_match'] for x in items}=={'exact','unassigned'}
    assert '不代表全国适用' in q.search_policy('安徽','交易')['notice']

def test_limit(sample):
    _,q,*_=sample
    result=q.search_policy('','交易',1)
    assert result['returned']==1 and result['has_more']

def test_no_match(sample):
    _,q,*_=sample
    assert q.search_policy('','不存在的关键词')['items']==[]

@pytest.mark.parametrize('keyword',['%', '_'])
def test_literal_metacharacters(sample,keyword):
    _,q,_,_,anhui,_=sample
    assert [x['policy_id'] for x in q.search_policy('',keyword)['items']]==[anhui.policy_id]

def test_parameterized(sample):
    store,q,*_=sample
    before=store.counts()
    assert q.search_policy('',"' OR 1=1; DROP TABLE policy_documents;--")['items']==[]
    assert store.counts()==before

def test_history_and_provenance(sample):
    _,q,a,b,*_=sample
    latest=q.get_policy_detail(a.policy_id)
    old=q.get_policy_detail(a.policy_id,1)
    assert latest['version']==2 and latest['document_id']==b.document_id
    assert latest['fetch_id']=='f3' and latest['observation_count']==2
    assert latest['last_observed_at']>latest['document_retrieved_at']
    assert old['fetch_id']=='f1' and old['version']==1 and not old['is_latest_observed']
    assert '旧规则' in old['body_text'] and '新规则' in latest['body_text']
    assert len(old['raw_sha256'])==64 and len(old['content_sha256'])==64
    json.dumps(latest,ensure_ascii=False)

@pytest.mark.parametrize('kwargs',[{'region':'','keyword':'  '},{'region':'','keyword':'a','limit':0},{'region':'','keyword':'a','limit':11},{'region':'','keyword':'a','limit':True}])
def test_invalid_search(sample,kwargs):
    with pytest.raises(ValidationError): sample[1].search_policy(**kwargs)

def test_invalid_id(sample):
    with pytest.raises(ValidationError): sample[1].get_policy_detail('bad')

def test_missing_id_or_version(sample):
    _,q,a,*_=sample
    for pid,v in [('0'*32,None),(a.policy_id,99)]:
        with pytest.raises(PolicyQueryError,match='POLICY_VERSION_NOT_FOUND'):q.get_policy_detail(pid,v)

def test_missing_db(tmp_path):
    path=tmp_path/'absent.db';q=PolicyQueryService(path)
    with pytest.raises(PolicyQueryError,match='POLICY_DB_NOT_FOUND'):q.search_policy('','x')
    assert not path.exists()

def test_wrong_schema(tmp_path):
    path=tmp_path/'wrong.db';sqlite3.connect(path).close()
    with pytest.raises(PolicyQueryError,match='POLICY_DB_READ_ERROR'):PolicyQueryService(path).search_policy('','x')

def test_readonly(sample):
    store,q,*_=sample
    before=store.counts();q.search_policy('','电力')
    with pytest.raises(PolicyQueryError):
        with q._read() as conn:conn.execute('DELETE FROM policy_documents')
    assert store.counts()==before

def test_pagination(tmp_path):
    store=VersionedPolicyStore(tmp_path/'db');body='电力政策'*5000
    result=store.ingest(raw(text=body),PROFILE);q=PolicyQueryService(store.db_path)
    parts=[];offset=0
    while offset is not None:
        detail=q.get_policy_detail(result.policy_id,result.version,offset)
        parts.append(detail['body_text']);offset=detail['next_offset']
        assert len(parts[-1])<=8000
    assert ''.join(parts)==body
    with pytest.raises(PolicyQueryError,match='BODY_OFFSET_OUT_OF_RANGE'):q.get_policy_detail(result.policy_id,1,len(body))

def test_bindings_mock_dispatch(sample):
    _,q,a,b,*_=sample
    bindings={x.name:x for x in build_policy_bindings(q)}
    # Emulates argument validation and dispatch, NOT the project's actual registry/executor.
    def dispatch(name,arguments):
        binding=bindings[name];validated=binding.args_model.model_validate(arguments)
        return binding.handler(**validated.model_dump())
    model=Mock(side_effect=[('search_policy',{'region':'','keyword':'新规则'}),('get_policy_detail',{'policy_id':a.policy_id,'version':2})])
    name,args=model();summary=dispatch(name,args);assert summary['items'][0]['document_id']==b.document_id
    name,args=model();detail=dispatch(name,args);assert '新规则' in detail['body_text']
    with pytest.raises(ValidationError):dispatch('get_policy_detail',{'policy_id':a.policy_id,'sql':'DROP TABLE'})
