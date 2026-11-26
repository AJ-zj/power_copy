import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from pypdf import PdfReader, PdfWriter
from powercopy.data.parser import RawPage, ParseProfile
from powercopy.data.pdf_ingest import PdfMetadata, import_pdf, load_pdf_artifact, preview_pdf, ingest_pdf
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore
from powercopy.rag.chunking import ChunkConfig, ChunkError, split_spans, build_manifest, digest
from scripts.build_chunks import main

BASE = datetime(2026, 11, 25, tzinfo=timezone.utc)
PROFILE = ParseProfile(title_selector='h1', body_selector='article')
FIXTURE = Path(__file__).parent/'fixtures'/'synthetic_policy_zh.pdf'


def raw(fid='f1', text='第一条 交易周期为15分钟。'*100, day=0, noise='', url='https://example.invalid/a'):
    body=f'<h1>模拟政策</h1><article><p>{text}</p></article><footer>{noise}</footer>'.encode()
    return RawPage(fetch_id=fid,source_key='demo',target_key='a',source_url=url,
        content_kind='policy',status_code=200,content_type='text/html',charset='utf-8',
        retrieved_at=BASE+timedelta(days=day),body_size=len(body),body_base64=base64.b64encode(body).decode())


@pytest.fixture
def store(tmp_path):
    return VersionedPolicyStore(tmp_path/'test.db')


@pytest.mark.parametrize('max_chars,overlap',[(31,0),(4097,0),(100,26),(100,-1),(True,0),(100,False)])
def test_config(max_chars,overlap):
    with pytest.raises(ChunkError,match='INVALID_CHUNK_CONFIG'):
        ChunkConfig(max_chars,overlap)


@pytest.mark.parametrize('text',['甲'*257, '第一条 交易周期为15分钟。'*30,'甲'*28+'\n'+'乙'*100,'短文','甲'*32])
def test_spans(text):
    cfg=ChunkConfig(32,8)
    spans=list(split_spans(text,cfg))
    assert spans[0].start==0 and spans[-1].end==len(text)
    for span in spans: assert 0 < span.end-span.start <= 32
    for a,b in zip(spans,spans[1:]):
        assert b.start == a.end-8 and b.end>a.end


def test_boundary_and_empty():
    spans=list(split_spans('甲'*27+'\n'+'乙'*100,ChunkConfig(32,8)))
    assert spans[0].end==28
    with pytest.raises(ChunkError,match='EMPTY_TEXT'): list(split_spans('  ',ChunkConfig()))


def test_exact_and_repeat(store,tmp_path):
    r=store.ingest(raw(),PROFILE)
    before=store.db_path.read_bytes()
    m=build_manifest(store.db_path,ChunkConfig(64,16),raw_pdf_dir=tmp_path/'raw_pdf')
    assert m==build_manifest(store.db_path,ChunkConfig(64,16),raw_pdf_dir=tmp_path/'raw_pdf')
    assert m['manifest_sha256']==digest({k:v for k,v in m.items() if k!='manifest_sha256'})
    doc=PolicyRepository(store.db_path).get(r.document_id)
    ids=set(); covered=set()
    for c in m['chunks']:
        assert c['text']==doc.body_text[c['start']:c['end']]
        assert c['text_sha256']==hashlib.sha256(c['text'].encode()).hexdigest()
        assert c['page'] is None and c['document_id']==r.document_id and c['reference_fetch_id']=='f1'
        ids.add(c['chunk_id']); covered.update(range(c['start'],c['end']))
    assert len(ids)==len(m['chunks']) and covered==set(range(len(doc.body_text)))
    # No byte-level write; repository constructor above only ensures existing table.
    assert store.db_path.read_bytes()==before
    other=build_manifest(store.db_path,ChunkConfig(64,0),raw_pdf_dir=tmp_path/'raw_pdf')
    assert not ids.intersection(c['chunk_id'] for c in other['chunks'])


def test_latest_and_unchanged(store,tmp_path):
    a=store.ingest(raw(text='旧内容'),PROFILE)
    b=store.ingest(raw('f2','新内容',1),PROFILE)
    m=build_manifest(store.db_path,raw_pdf_dir=tmp_path/'raw_pdf')
    assert [c['document_id'] for c in m['chunks']]==[b.document_id]
    assert m['chunks'][0]['version']==2 and m['chunks'][0]['reference_fetch_id']=='f2'
    store.ingest(raw('f3','新内容',2,noise='页脚变化'),PROFILE)
    assert build_manifest(store.db_path,raw_pdf_dir=tmp_path/'raw_pdf')==m
    c=store.ingest(raw('f4','旧内容',3),PROFILE)
    n=build_manifest(store.db_path,raw_pdf_dir=tmp_path/'raw_pdf')
    assert c.content_sha256==a.content_sha256 and n['chunks'][0]['document_id']==c.document_id
    assert n['chunks'][0]['chunk_id']!=m['chunks'][0]['chunk_id']


def test_missing_db_and_empty(store,tmp_path):
    p=tmp_path/'missing.db'
    with pytest.raises(ChunkError,match='POLICY_DB_NOT_FOUND'): build_manifest(p)
    assert not p.exists()
    assert build_manifest(store.db_path)['chunks']==[]


def test_corruption(store):
    r=store.ingest(raw(),PROFILE)
    with sqlite3.connect(store.db_path) as c:
        c.execute('UPDATE policy_documents SET body_text=? WHERE document_id=?',('bad',r.document_id))
    with pytest.raises(ChunkError,match='CONTENT_HASH_MISMATCH'): build_manifest(store.db_path)


def add_pdf(store,tmp_path,single=False):
    path=FIXTURE
    if single:
        path=tmp_path/'one.pdf'
        writer=PdfWriter(); writer.add_page(PdfReader(FIXTURE).pages[0]); writer.write(path)
    meta=PdfMetadata(source_key='nea_east',target_key='fixture',source_url='https://hdj.nea.gov.cn/fixture.pdf',
        title='模拟政策，非真实政策',retrieved_at=BASE,provenance_note='Synthetic fixture only')
    p=import_pdf(path,meta,tmp_path/'raw_pdf')
    a=load_pdf_artifact(p); preview=preview_pdf(a)
    r=ingest_pdf(store,a,approved_sha256=preview.review_sha256)
    return p,r,preview


@pytest.mark.parametrize('single',[False,True])
def test_pdf_pages(store,tmp_path,single):
    p,r,preview=add_pdf(store,tmp_path,single)
    m=build_manifest(store.db_path,ChunkConfig(32,8),raw_pdf_dir=p.parent)
    assert {c['page'] for c in m['chunks']}=={pg.number for pg in preview.pages}
    for c in m['chunks']:
        pg=preview.pages[c['page']-1]
        assert pg.start <= c['start'] < c['end'] <= pg.end
        assert c['text']==preview.document.body_text[c['start']:c['end']]
        assert c['reference_fetch_id']==r.fetch_id and '\f' not in c['text']


def test_pdf_missing_archive(store,tmp_path):
    p,_,_=add_pdf(store,tmp_path)
    p.unlink()
    with pytest.raises(ChunkError,match='PDF_ARTIFACT_REQUIRED'): build_manifest(store.db_path,raw_pdf_dir=p.parent)


def test_pdf_tampered_archive(store,tmp_path):
    p,_,_=add_pdf(store,tmp_path)
    data=json.loads(p.read_text(encoding='utf-8')); data['metadata']['title']='tampered'; p.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ChunkError,match='PDF_PROVENANCE_MISMATCH'): build_manifest(store.db_path,raw_pdf_dir=p.parent)


def test_cli_output(store,tmp_path,monkeypatch):
    store.ingest(raw(),PROFILE)
    out=tmp_path/'chunks.json'
    monkeypatch.setattr(sys,'argv',['build_chunks','--db',str(store.db_path),'--out',str(out),'--raw-pdf-dir',str(tmp_path/'raw_pdf')])
    main(); data=out.read_bytes(); main(); assert out.read_bytes()==data
    out.write_text('{}',encoding='utf-8')
    with pytest.raises(SystemExit): main()
    assert out.read_text(encoding='utf-8')=='{}'
