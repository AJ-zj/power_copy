import base64
from datetime import datetime, timezone, timedelta
from io import BytesIO
import sqlite3

import pytest
from pypdf import PdfWriter
from powercopy.data.pdf_ingest import (PdfMetadata, PdfArtifact, PdfIngestError,
    import_pdf, load_pdf_artifact, preview_pdf, ingest_pdf, PAGE_BREAK, _sha)
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore, VersionError
from powercopy.data.query import PolicyQueryService

# Minimal PDF with built-in Helvetica; no extra test dependencies.
def pdf_bytes(*texts, encrypted=False):
    from pypdf.generic import (DictionaryObject, NameObject, DecodedStreamObject)
    writer = PdfWriter()
    for text in texts:
        page=writer.add_blank_page(width=300,height=300)
        if text:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                 NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            stream=DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 20 200 Td ('+text.encode('ascii')+b') Tj ET')
            page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted: writer.encrypt('test-password')
    buf=BytesIO(); writer.write(buf); return buf.getvalue()

BASE=datetime(2026,11,25,tzinfo=timezone.utc)
def metadata(**changes):
    return PdfMetadata(**(dict(source_key='nea_east',target_key='fixture',
        source_url='https://hdj.nea.gov.cn/test-fixture.pdf',title='模拟政策，仅供测试',
        retrieved_at=BASE,provenance_note='Synthetic PDF fixture, not official')|changes))

def artifact(body=None, fid='a'*32, **changes):
    body=pdf_bytes('Article 1: 15 minutes.','Article 2: 3.0 yuan/kWh.') if body is None else body
    return PdfArtifact(fetch_id=fid,metadata=metadata(**changes),raw_sha256=_sha(body),
        body_size=len(body),body_base64=base64.b64encode(body).decode())

def commit(store, raw):
    return ingest_pdf(store,raw,approved_sha256=preview_pdf(raw).review_sha256)

@pytest.fixture
def store(tmp_path): return VersionedPolicyStore(tmp_path/'policies.db')

def test_page_boundaries():
    p=preview_pdf(artifact())
    assert len(p.pages)==2 and p.pages[0].number==1 and p.pages[1].number==2
    assert p.document.body_text=='Article 1: 15 minutes.'+PAGE_BREAK+'Article 2: 3.0 yuan/kWh.'
    for page in p.pages: assert p.document.body_text[page.start:page.end]==page.text
    assert p.document.published_on is None and p.document.issuer is None

@pytest.mark.parametrize('body,code',[(b'not a pdf','NOT_PDF'),(b'%PDF-1.7\ninvalid','PDF_PARSE_FAILED'),
    (pdf_bytes('abc',encrypted=True),'ENCRYPTED_PDF'),(pdf_bytes('abc',''),'PAGE_WITHOUT_TEXT:2'),
    (pdf_bytes(''),'PAGE_WITHOUT_TEXT:1')])
def test_reject(body,code):
    with pytest.raises(PdfIngestError,match=code): preview_pdf(artifact(body))

@pytest.mark.parametrize('url',['https://hdj.nea.gov.cn.evil.invalid/a.pdf','http://hdj.nea.gov.cn/a.pdf','https://user@hdj.nea.gov.cn/a.pdf','https://hdj.nea.gov.cn:8443/a.pdf'])
def test_source(url):
    with pytest.raises(PdfIngestError,match='UNAPPROVED_SOURCE_URL'): preview_pdf(artifact(source_url=url))

def test_integrity():
    a=artifact().model_copy(update={'raw_sha256':'0'*64})
    with pytest.raises(PdfIngestError,match='RAW_INTEGRITY_MISMATCH'): preview_pdf(a)

def test_import_roundtrip(tmp_path):
    pdf=tmp_path/'test.pdf'; pdf.write_bytes(pdf_bytes('text'))
    path=import_pdf(pdf,metadata(),tmp_path/'raw_pdf')
    a=load_pdf_artifact(path); assert a.body_bytes()==pdf.read_bytes()
    assert path.stem==a.fetch_id and a.acquisition_kind=='manual_offline'

def test_approval_and_stale(store):
    a=artifact(); p=preview_pdf(a)
    with pytest.raises(PdfIngestError,match='REVIEW_REQUIRED_OR_STALE'): ingest_pdf(store,a,approved_sha256='x')
    changed=a.model_copy(update={'metadata':metadata(title='different title')})
    with pytest.raises(PdfIngestError,match='REVIEW_REQUIRED_OR_STALE'): ingest_pdf(store,changed,approved_sha256=p.review_sha256)
    assert store.counts()['documents']==0

def test_versions(store):
    a=artifact(pdf_bytes('A')); b=artifact(pdf_bytes('B'),fid='b'*32,retrieved_at=BASE+timedelta(days=1))
    c=artifact(a.body_bytes(),fid='c'*32,retrieved_at=BASE+timedelta(days=2))
    r1,r2,r3=[commit(store,x) for x in (a,b,c)]
    assert [r1.version,r2.version,r3.version]==[1,2,3]
    assert r1.content_sha256==r3.content_sha256!=r2.content_sha256
    assert commit(store,c).action=='replayed'
    assert store.counts()==dict(policies=1,versions=3,fetches=3,documents=3)
    assert store.observations(r1.policy_id)[0]['raw_sha256']==a.raw_sha256

def test_unchanged(store):
    a=artifact(); r=commit(store,a)
    b=artifact(a.body_bytes(),fid='b'*32,retrieved_at=BASE+timedelta(days=1))
    s=commit(store,b)
    assert s.action=='unchanged' and r.document_id==s.document_id
    assert store.counts()['fetches']==2

def test_conflict(store):
    a=artifact(); commit(store,a)
    with pytest.raises(VersionError,match='FETCH_ID_CONFLICT'): commit(store,artifact(pdf_bytes('changed')))

def test_rollback(store):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("CREATE TRIGGER fail BEFORE INSERT ON policy_fetches BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError): commit(store,artifact())
    assert store.counts()==dict(policies=0,versions=0,fetches=0,documents=0)

def test_existing_read_path(store):
    a=artifact(); r=commit(store,a); p=preview_pdf(a)
    assert PolicyRepository(store.db_path).get(r.document_id).body_text==p.document.body_text
    s=PolicyQueryService(store.db_path)
    assert s.search_policy('', '模拟政策')['items'][0]['policy_id']==r.policy_id
    detail=s.get_policy_detail(r.policy_id,version=1)
    assert detail['raw_sha256']==a.raw_sha256

def test_page_limit(monkeypatch):
    import powercopy.data.pdf_ingest as module
    monkeypatch.setattr(module,'MAX_PAGES',1)
    with pytest.raises(PdfIngestError,match='PAGE_LIMIT'): preview_pdf(artifact())

def test_oversize_import(tmp_path,monkeypatch):
    import powercopy.data.pdf_ingest as module
    monkeypatch.setattr(module,'MAX_BYTES',10)
    path=tmp_path/'x.pdf'; path.write_bytes(pdf_bytes('abc'))
    with pytest.raises(PdfIngestError,match='PDF_SIZE_LIMIT'): import_pdf(path,metadata(),tmp_path/'raw')


def test_chinese_fixture(store):
    from pathlib import Path
    a=artifact((Path(__file__).parent/'fixtures'/'synthetic_policy_zh.pdf').read_bytes())
    p=preview_pdf(a)
    assert '第一条 交易周期为15分钟。' in p.pages[0].text
    assert '第二条 电价为3元/千瓦时。' in p.pages[1].text
    r=commit(store,a)
    assert PolicyRepository(store.db_path).get(r.document_id).body_text==p.document.body_text


def test_html_fingerprint_unchanged(store):
    # Explicit legacy formula, unchanged by transaction extraction.
    from test_policy_versioning import raw, PROFILE
    from powercopy.data.parser import parse_article, to_policy_document
    from powercopy.data.versioning import _json_hash, _utc, content_hash
    a=raw(); doc=to_policy_document(a,parse_article(a,PROFILE))
    legacy=_json_hash({'source_key':doc.source_key,'source_url':str(doc.source_url),
        'target_key':a.target_key,'retrieved_at':_utc(a.retrieved_at),
        'raw_sha256':_sha(a.body_bytes()),'content_sha256':content_hash(doc),
        'content_kind':a.content_kind,'status_code':a.status_code,'content_type':a.content_type,
        'charset':a.charset,'profile':PROFILE.model_dump(mode='json')})
    r=store.ingest(a,PROFILE)
    assert store.observations(r.policy_id)[0]['input_sha256']==legacy
    assert store.ingest(a,PROFILE).action=='replayed'
    commit(store,artifact())
    assert store.counts()==dict(policies=2,versions=2,fetches=2,documents=2)


def test_pdf_out_of_order(store):
    commit(store,artifact(retrieved_at=BASE+timedelta(days=1)))
    with pytest.raises(VersionError,match='OUT_OF_ORDER_FETCH'):
        commit(store,artifact(pdf_bytes('older'),fid='b'*32))
