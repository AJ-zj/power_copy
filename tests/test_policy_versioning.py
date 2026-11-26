import base64
from datetime import datetime, timezone, timedelta
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from powercopy.data.parser import RawPage, ParseProfile
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore, VersionError


PROFILE = ParseProfile(title_selector='h1', body_selector='article')
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def raw(fid='f1', text='第一条 A', day=0, url='https://example.invalid/policy/1',
        source='demo', noise='', kind='policy', title='政策', target='detail'):
    html = f'<html><h1>{title}</h1><article><p>{text}</p></article><footer>{noise}</footer></html>'.encode()
    return RawPage(fetch_id=fid, source_key=source, target_key=target,
                   source_url=url, content_kind=kind, status_code=200,
                   content_type='text/html', charset='utf-8', retrieved_at=BASE+timedelta(days=day),
                   body_size=len(html), body_base64=base64.b64encode(html).decode())


@pytest.fixture
def store(tmp_path):
    return VersionedPolicyStore(tmp_path/'versioned.db')


def test_first(store):
    result = store.ingest(raw(), PROFILE)
    assert result.action == 'created' and result.version == 1
    assert store.counts() == dict(policies=1, versions=1, fetches=1, documents=1)
    assert PolicyRepository(store.db_path).get(result.document_id).body_text == '第一条 A'
    assert len(store.observations(result.policy_id)[0]['raw_sha256']) == 64


def test_replay(store):
    first = store.ingest(raw(), PROFILE)
    second = store.ingest(raw(), PROFILE)
    assert second.action == 'replayed' and first.document_id == second.document_id
    assert store.counts()['fetches'] == 1


def test_unchanged_with_html_noise(store):
    first = store.ingest(raw(), PROFILE)
    second = store.ingest(raw('f2', day=1, noise='new footer'), PROFILE)
    assert second.action == 'unchanged' and second.document_id == first.document_id
    assert store.history(first.policy_id)[0]['observations'] == 2
    obs = store.observations(first.policy_id)
    assert obs[0]['raw_sha256'] != obs[1]['raw_sha256']
    assert store.counts() == dict(policies=1, versions=1, fetches=2, documents=1)


def test_change_and_recurrence(store):
    a = store.ingest(raw(), PROFILE)
    b = store.ingest(raw('f2', '第一条 B', 1), PROFILE)
    c = store.ingest(raw('f3', '第一条 A', 2), PROFILE)
    assert [a.version,b.version,c.version] == [1,2,3]
    assert a.policy_id == b.policy_id == c.policy_id
    assert a.content_sha256 == c.content_sha256 != b.content_sha256
    assert len({a.document_id,b.document_id,c.document_id}) == 3
    assert PolicyRepository(store.db_path).get(a.document_id).body_text == '第一条 A'
    assert store.ingest(raw(), PROFILE).version == 1


@pytest.mark.parametrize('kwargs',[{'text':'changed'},{'noise':'changed'}, {'day':1}, {'target':'other'}, {'url':'https://example.invalid/2'}])
def test_fetch_id_conflict(store,kwargs):
    store.ingest(raw(), PROFILE)
    with pytest.raises(VersionError,match='FETCH_ID_CONFLICT'):
        store.ingest(raw(**kwargs), PROFILE)
    assert store.counts()['fetches']==1


def test_profile_change_conflict(store):
    store.ingest(raw(), PROFILE)
    with pytest.raises(VersionError,match='FETCH_ID_CONFLICT'):
        store.ingest(raw(), ParseProfile(title_selector='h1', body_selector='article',region='安徽'))


def test_metadata_change_new_version(store):
    first = store.ingest(raw(), PROFILE)
    second = store.ingest(raw('f2', day=1), ParseProfile(title_selector='h1',body_selector='article',region='安徽'))
    assert second.version==2 and second.content_sha256!=first.content_sha256


def test_different_identity(store):
    a=store.ingest(raw(),PROFILE)
    b=store.ingest(raw('f2',url='https://example.invalid/2'),PROFILE)
    c=store.ingest(raw('f3',source='other'),PROFILE)
    assert len({a.policy_id,b.policy_id,c.policy_id})==3
    assert store.counts()['versions']==3


def test_out_of_order(store):
    store.ingest(raw(day=2), PROFILE)
    with pytest.raises(VersionError,match='OUT_OF_ORDER_FETCH'):
        store.ingest(raw('late','old',1),PROFILE)
    assert store.counts()['fetches']==1


def test_same_time_conflicting(store):
    store.ingest(raw(),PROFILE)
    with pytest.raises(VersionError,match='AMBIGUOUS_FETCH_ORDER'):
        store.ingest(raw('other','changed'),PROFILE)
    assert store.ingest(raw('same'),PROFILE).action=='unchanged'


@pytest.mark.parametrize('kind',['market_news','index'])
def test_other_content_not_ingested(store,kind):
    from powercopy.data.normalizer import ParseError
    with pytest.raises(ParseError):
        store.ingest(raw(kind=kind),PROFILE)
    assert store.counts()==dict(policies=0,versions=0,fetches=0,documents=0)


def test_rollback(store):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("CREATE TRIGGER reject_fetch BEFORE INSERT ON policy_fetches BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest(raw(),PROFILE)
    assert store.counts()==dict(policies=0,versions=0,fetches=0,documents=0)


def test_restart(store):
    a=store.ingest(raw(),PROFILE)
    other=VersionedPolicyStore(store.db_path)
    b=other.ingest(raw('f2',day=1),PROFILE)
    assert a.document_id==b.document_id


def test_legacy_guard(tmp_path):
    from powercopy.data.parser import parse_article,to_policy_document
    repo=PolicyRepository(tmp_path/'legacy.db')
    doc=to_policy_document(raw(),parse_article(raw(),PROFILE))
    repo.add(doc)
    with pytest.raises(VersionError,match='UNTRACKED_DOCUMENTS'):
        VersionedPolicyStore(repo.db_path)
    assert repo.get(doc.document_id)==doc


def test_concurrent_replay(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:store.ingest(raw(),PROFILE),range(2)))
    assert sorted(r.action for r in results)==['created','replayed']
    assert store.counts()['fetches']==1


def test_concurrent_same_content(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda i:store.ingest(raw(f'f{i}'),PROFILE),range(2)))
    assert sorted(r.action for r in results)==['created','unchanged']
    assert store.counts()==dict(policies=1,versions=1,fetches=2,documents=1)


def test_thirty_days(store):
    for day in range(30):
        store.ingest(raw(str(day),'A' if day<14 else 'B',day),PROFILE)
    assert store.counts()==dict(policies=1,versions=2,fetches=30,documents=2)
