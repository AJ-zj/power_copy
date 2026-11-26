"""Offline text PDF ingestion. No network, OCR, or automatic source approval."""
import base64
import binascii
from dataclasses import dataclass
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import re
from uuid import uuid4

from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field
from pypdf import PdfReader, __version__ as PYPDF_VERSION
from powercopy.data.models import PolicyDocument
from powercopy.data.versioning import VersionedPolicyStore, _sha, _json_hash, _utc, content_hash

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
MAX_PAGE_CHARS = 100_000
MAX_CHARS = 500_000
PAGE_BREAK = '\n\f\n'
HOSTS = {'ndrc': 'zfxxgk.ndrc.gov.cn', 'nea': 'zfxxgk.nea.gov.cn',
         'nea_east': 'hdj.nea.gov.cn'}

class PdfIngestError(ValueError):
    pass

class PdfMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    source_key: str
    target_key: str = Field(min_length=1, max_length=150)
    source_url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=500)
    issuer: str | None = None
    region: str | None = None
    published_on: date | None = None
    retrieved_at: AwareDatetime
    provenance_note: str = Field(min_length=1, max_length=1000)

    def check_source(self):
        u = self.source_url
        if (self.source_key not in HOSTS or u.host != HOSTS[self.source_key]
            or u.scheme != 'https' or u.port not in (None, 443)
            or u.username is not None or u.password is not None or u.fragment is not None):
            raise PdfIngestError('UNAPPROVED_SOURCE_URL')

class PdfArtifact(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    fetch_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    acquisition_kind: str = 'manual_offline'
    metadata: PdfMetadata
    raw_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    body_size: int = Field(gt=0, le=MAX_BYTES)
    body_base64: str = Field(max_length=4*((MAX_BYTES+2)//3))

    def body_bytes(self):
        if self.acquisition_kind != 'manual_offline':
            raise PdfIngestError('INVALID_ACQUISITION_KIND')
        self.metadata.check_source()
        try:
            body = base64.b64decode(self.body_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PdfIngestError('INVALID_BASE64') from exc
        if len(body) != self.body_size or _sha(body) != self.raw_sha256:
            raise PdfIngestError('RAW_INTEGRITY_MISMATCH')
        if not body.startswith(b'%PDF-'):
            raise PdfIngestError('NOT_PDF')
        return body

def import_pdf(path, metadata: PdfMetadata, raw_dir):
    """Generate event once; replay this JSON, not the import command."""
    metadata.check_source()
    with Path(path).open('rb') as f:
        body = f.read(MAX_BYTES+1)
    if not 0 < len(body) <= MAX_BYTES:
        raise PdfIngestError('PDF_SIZE_LIMIT')
    artifact = PdfArtifact(fetch_id=uuid4().hex, metadata=metadata,
        raw_sha256=_sha(body), body_size=len(body), body_base64=base64.b64encode(body).decode('ascii'))
    artifact.body_bytes()
    root = Path(raw_dir); root.mkdir(parents=True, exist_ok=True)
    dest = root / (artifact.fetch_id + '.json')
    temp = dest.with_suffix('.tmp')
    try:
        with temp.open('x', encoding='utf-8') as f:
            f.write(artifact.model_dump_json(indent=2))
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)
    return dest

def load_pdf_artifact(path):
    with Path(path).open('rb') as f:
        data = f.read(4*((MAX_BYTES+2)//3)+64_001)
    if len(data) > 4*((MAX_BYTES+2)//3)+64_000:
        raise PdfIngestError('ARTIFACT_TOO_LARGE')
    artifact = PdfArtifact.model_validate_json(data)
    artifact.body_bytes()
    return artifact

@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str
    start: int
    end: int

@dataclass(frozen=True)
class PdfPreview:
    document: PolicyDocument
    pages: tuple[PdfPage, ...]
    review_sha256: str
    content_sha256: str
    raw_sha256: str
    parser_version: str


def preview_pdf(artifact: PdfArtifact):
    body = artifact.body_bytes()
    try:
        reader = PdfReader(BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise PdfIngestError('ENCRYPTED_PDF')
        if not 0 < len(reader.pages) <= MAX_PAGES:
            raise PdfIngestError('PAGE_LIMIT')
        pages = []; cursor = 0
        for number, page in enumerate(reader.pages, 1):
            # Do not remove headers, footers, numeric punctuation, or guess reading order.
            text = (page.extract_text() or '').replace('\r\n','\n').replace('\r','\n')
            if '\f' in text or '\x00' in text or '\ufffd' in text:
                raise PdfIngestError(f'INVALID_PAGE_TEXT:{number}')
            text = '\n'.join(line.rstrip() for line in text.split('\n')).strip()
            if not text:
                raise PdfIngestError(f'PAGE_WITHOUT_TEXT:{number}')
            if len(text) > MAX_PAGE_CHARS or cursor + len(text) > MAX_CHARS:
                raise PdfIngestError('TEXT_LIMIT')
            pages.append(PdfPage(number, text, cursor, cursor+len(text)))
            cursor += len(text)+len(PAGE_BREAK)
    except PdfIngestError:
        raise
    except Exception as exc:
        raise PdfIngestError('PDF_PARSE_FAILED') from exc
    m = artifact.metadata
    doc = PolicyDocument(source_key=m.source_key, source_url=m.source_url,
        title=m.title, issuer=m.issuer, region=m.region, published_on=m.published_on,
        retrieved_at=m.retrieved_at, body_text=PAGE_BREAK.join(p.text for p in pages))
    digest = content_hash(doc)
    version = 'pdf-text-v1/pypdf-'+PYPDF_VERSION
    # Bind human review to exact input, extracted text, metadata and parser version.
    fingerprint = _json_hash({'fetch_id':artifact.fetch_id, 'raw_sha256':artifact.raw_sha256,
        'content_sha256':digest, 'metadata':m.model_dump(mode='json'), 'parser':version,
        'page_ranges':[(p.number,p.start,p.end) for p in pages], 'kind':artifact.acquisition_kind})
    return PdfPreview(doc,tuple(pages),fingerprint,digest,artifact.raw_sha256,version)


def ingest_pdf(store: VersionedPolicyStore, artifact: PdfArtifact, *, approved_sha256: str):
    preview = preview_pdf(artifact)
    if approved_sha256 != preview.review_sha256:
        raise PdfIngestError('REVIEW_REQUIRED_OR_STALE')
    return store._ingest_prepared(preview.document, fetch_id=artifact.fetch_id,
        target_key=artifact.metadata.target_key, stamp=_utc(artifact.metadata.retrieved_at),
        raw_sha=preview.raw_sha256, digest=preview.content_sha256, fingerprint=preview.review_sha256)
