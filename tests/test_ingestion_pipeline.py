from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import base64
import json

import pytest
from powercopy.data.parser import RawPage
from powercopy.data.pipeline import IngestionPipeline, discover, atomic_json
from powercopy.data.versioning import VersionedPolicyStore

URL='https://example.invalid/policy/1'
KEY='demo/detail'
PROFILE={KEY:dict(title_selector='h1',body_selector='article')}

@dataclass
class Response:
    source_key: str='demo'
    target_key: str='detail'
    source_url: str=URL
    content_kind: str='policy'
    status_code: int=200
    content_type: str='text/html'
    charset: str='utf-8'
    retrieved_at: datetime=datetime(2026,11,25,tzinfo=timezone.utc)
    duration_ms: float=1
    body: bytes=b'<h1>Policy</h1><article>Article A</article>'

class Error(Exception):
    def __init__(self,code,status=None):
        self.code,self.status_code=code,status

@pytest.fixture
def env(tmp_path):
    store=VersionedPolicyStore(tmp_path/'test.db')
    registry=Mock()
    registry.resolve.return_value=(None,SimpleNamespace(url=URL,content_kind='policy'))
    fetcher=Mock()
    fetcher.fetch.return_value=Response()
    ticks=[0.0]
    def wait(n): ticks[0]+=n
    pipe=IngestionPipeline(store,PROFILE,raw_dir=tmp_path/'raw',run_dir=tmp_path/'runs',
        registry=registry,fetcher=fetcher,approvals={KEY:dict(url=URL,approved=True,evidence='test only')},
        clock=lambda:ticks[0],sleeper=Mock(side_effect=wait))
    return pipe,fetcher,store

def live(pipe,n=1): return pipe.live([('demo','detail')]*n)

def test_live_replay(env):
    p,f,s=env
    r=live(p)
    assert r['status']=='success' and r['requests']==1
    i=r['items'][0]
    assert i['result']['action']=='created'
    report=p.replay([i['raw_path']])
    assert report['requests']==0 and report['items'][0]['result']['action']=='replayed'
    assert f.fetch.call_count==1 and s.counts()['fetches']==1
    # 必须显式 encoding：报告里含中文路径，Windows 默认 cp936 读回会不一致
    assert json.loads((p.run_dir/(r['run_id']+'.json')).read_text(encoding='utf-8'))==r

def test_unchanged(env):
    p,f,s=env
    r=live(p,2)
    assert [i['result']['action'] for i in r['items']]==['created','unchanged']
    assert s.counts()['fetches']==2 and s.counts()['versions']==1
    assert p.sleeper.call_args_list[0].args[0]==2

@pytest.mark.parametrize('code,status',[('http_503',503),('TIMEOUT',None),('network_error',None)])
def test_retry(env,code,status):
    p,f,s=env
    f.fetch.side_effect=[Error(code,status),Response()]
    r=live(p)
    assert r['requests']==2 and r['status']=='success'
    assert [a['status'] for a in r['items'][0]['attempts']]==['failed','success']
    assert p.sleeper.call_args_list[0].args[0]==2

@pytest.mark.parametrize('status',[403,429])
def test_pause(env,status):
    p,f,s=env
    f.fetch.side_effect=Error(f'http_{status}',status)
    r=live(p,2)
    assert r['requests']==1 and r['status']=='failed'
    assert r['items'][1]['error_code']=='RUN_PAUSED'

@pytest.mark.parametrize('code,status',[('http_404',404),('response_too_large',200),('redirect_not_allowed',302)])
def test_no_retry(env,code,status):
    p,f,s=env
    f.fetch.side_effect=Error(code,status)
    assert live(p)['requests']==1

@pytest.mark.parametrize('approval',[{}, {KEY:dict(url='https://evil.invalid',approved=True,evidence='x')},{KEY:dict(url=URL,approved=True)}])
def test_approval(env,approval):
    p,f,s=env;p.approvals=approval
    r=live(p)
    assert r['requests']==0 and r['items'][0]['error_code']=='NO_OPERATOR_APPROVAL'
    f.fetch.assert_not_called()

def test_budget(env):
    p,f,s=env;p.max_requests=1
    r=live(p,2)
    assert r['requests']==1 and r['status']=='partial'
    assert r['items'][1]['error_code']=='REQUEST_BUDGET_EXHAUSTED'

def test_retry_budget(env):
    p,f,s=env;p.max_requests=1
    f.fetch.side_effect=Error('timeout')
    assert live(p)['requests']==1

def test_retry_limit(env):
    p,f,s=env;f.fetch.side_effect=Error('http_503',503)
    assert live(p)['requests']==2

def test_parse_failure_keeps_raw_and_other_runs(env):
    p,f,s=env
    f.fetch.side_effect=[Response(body=b'<h2>Bad</h2>'),Response()]
    r=live(p,2)
    assert r['status']=='partial' and r['requests']==2
    assert Path(r['items'][0]['raw_path']).exists()
    assert r['items'][0]['error_code']=='SELECTOR_MATCH_COUNT'
    assert s.counts()['fetches']==1

def test_missing_profile(env):
    p,f,s=env;p.profiles={}
    assert live(p)['requests']==0

def test_mismatch(env):
    p,f,s=env;f.fetch.return_value=Response(source_url='https://evil.invalid')
    r=live(p)
    assert r['items'][0]['error_code']=='FETCH_RESULT_MISMATCH' and not p.raw_dir.exists()

def index_raw():
    b=b'<div><a href="/a">A</a><a href="/a">duplicate</a><a href="https://evil.invalid/a">evil</a><a href="http://example.invalid/a">http</a><a href="/b">B</a></div>'
    return RawPage(fetch_id='index1',source_key='demo',target_key='index',source_url='https://example.invalid/index',content_kind='index',status_code=200,content_type='text/html',charset='utf-8',retrieved_at=Response.retrieved_at,body_size=len(b),body_base64=base64.b64encode(b).decode())

def test_discovery(env,tmp_path):
    p,f,s=env
    raw=index_raw();path=tmp_path/'index.json'
    atomic_json(path,raw.model_dump(mode='json'))
    r=p.replay([path],{'demo/index':'div a'})
    assert [c['url'] for c in r['items'][0]['result']['candidates']]==['https://example.invalid/a','https://example.invalid/b']
    assert all(c['status']=='pending_review' for c in r['items'][0]['result']['candidates'])
    assert len(discover(raw,'div a',1))==1
    assert s.counts()['documents']==0 and r['requests']==0
    f.fetch.assert_not_called();p.registry.register.assert_not_called()

def test_corrupt_raw_failure(env,tmp_path):
    p,f,s=env
    path=tmp_path/'bad.json';path.write_text('invalid')
    assert p.replay([path])['status']=='failed'
    f.fetch.assert_not_called()


def test_updated(env):
    from datetime import timedelta
    p,f,s=env
    f.fetch.side_effect=[Response(),Response(body=b'<h1>Policy</h1><article>Article B</article>',retrieved_at=Response.retrieved_at+timedelta(days=1))]
    r=live(p,2)
    assert [i['result']['action'] for i in r['items']]==['created','updated']
    assert s.counts()==dict(policies=1,versions=2,fetches=2,documents=2)


def test_recover_without_refetch(env):
    p,f,s=env
    f.fetch.return_value=Response(body=b'<h1>Policy</h1><section>Article</section>')
    r=live(p)
    assert r['status']=='failed'
    p.profiles={KEY:dict(title_selector='h1',body_selector='section')}
    replay=p.replay([r['items'][0]['raw_path']])
    assert replay['status']=='success' and replay['requests']==0
    assert f.fetch.call_count==1 and s.counts()['documents']==1


def test_news_archive(env):
    p,f,s=env
    p.registry.resolve.return_value=(None,SimpleNamespace(url=URL,content_kind='market_news'))
    f.fetch.return_value=Response(content_kind='market_news')
    r=live(p)
    assert r['items'][0]['result']['action']=='archived_only' and s.counts()['documents']==0
    assert Path(r['items'][0]['raw_path']).exists()


def test_index_zero_match(env):
    p,f,s=env
    with pytest.raises(Exception,match='INDEX_SELECTOR_NO_MATCH'):
        discover(index_raw(),'#missing')


def test_live_index(env):
    p,f,s=env
    p.registry.resolve.return_value=(None,SimpleNamespace(url=URL,content_kind='index'))
    f.fetch.return_value=Response(content_kind='index',body=b'<div><a href="/a">A</a></div>')
    r=p.live([('demo','detail')],{KEY:'div a'})
    assert r['requests']==1 and r['items'][0]['result']['candidates'][0]['status']=='pending_review'
    assert s.counts()['documents']==0
    p.registry.register.assert_not_called()
