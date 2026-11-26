"""Deterministic, exact-slice policy chunks. No model calls or database writes."""
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import json
import sqlite3

from powercopy.data.models import PolicyDocument
from powercopy.data.versioning import content_hash, _utc
from powercopy.data.pdf_ingest import PAGE_BREAK, load_pdf_artifact

ALGORITHM = 'policy-char-v1'

class ChunkError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ChunkConfig:
    max_chars: int = 500
    overlap_chars: int = 80

    def __post_init__(self):
        if (type(self.max_chars) is not int or type(self.overlap_chars) is not int
            or not 32 <= self.max_chars <= 4096
            or not 0 <= self.overlap_chars <= self.max_chars // 4):
            raise ChunkError('INVALID_CHUNK_CONFIG')


@dataclass(frozen=True)
class Span:
    start: int
    end: int


def split_spans(text: str, config: ChunkConfig):
    """Prefer a paragraph/sentence end in the last quarter; hard cap in characters.
    The overlap is exact within a page; do not create a redundant final tail.
    """
    if not text.strip():
        raise ChunkError('EMPTY_TEXT')
    start = 0
    while start < len(text):
        end = min(start + config.max_chars, len(text))
        if end < len(text):
            floor = start + (config.max_chars * 3 + 3) // 4
            for marks in ('\n', '。！？；!?;'):
                candidates = [i+1 for i in range(floor-1, end) if text[i] in marks]
                if candidates:
                    end = candidates[-1]
                    break
        yield Span(start, end)
        if end == len(text):
            break
        start = end - config.overlap_chars


# Same read snapshot for all versions/observations. Reference is earliest observed
# event of the selected version, not a claim about legal validity or live freshness.
SQL = '''SELECT i.policy_id, v.version, v.content_sha256, d.*,
 f.fetch_id, f.raw_sha256, f.retrieved_at AS observed_at
 FROM policy_identities i JOIN policy_versions v USING(policy_id)
 JOIN policy_documents d ON d.document_id=v.document_id
 LEFT JOIN policy_fetches f ON f.fetch_id=(SELECT x.fetch_id FROM policy_fetches x
 WHERE x.policy_id=v.policy_id AND x.version=v.version
 ORDER BY x.retrieved_at, x.fetch_id LIMIT 1)
 WHERE v.version=(SELECT MAX(y.version) FROM policy_versions y WHERE y.policy_id=i.policy_id)
 ORDER BY i.policy_id'''


def read_latest(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise ChunkError('POLICY_DB_NOT_FOUND')
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            if conn.execute('''SELECT 1 FROM policy_documents d WHERE NOT EXISTS
                (SELECT 1 FROM policy_versions v WHERE v.document_id=d.document_id) LIMIT 1''').fetchone():
                raise ChunkError('UNTRACKED_DOCUMENTS')
            rows = [dict(row) for row in conn.execute(SQL)]
    except sqlite3.Error as exc:
        raise ChunkError('POLICY_DB_READ_ERROR') from exc
    return rows


def _page_ranges(doc, row, raw_pdf_dir):
    # The existing schema does not have a MIME field. Never infer physical pages
    # from the number of chunks. Verify a PDF artifact against the ledger first.
    fid = row['fetch_id']
    path = None
    if len(fid) == 32 and all(c in '0123456789abcdef' for c in fid):
        path = Path(raw_pdf_dir) / (fid+'.json')
    if path is not None and path.is_file():
        try:
            artifact = load_pdf_artifact(path)
            m = artifact.metadata
            if (artifact.fetch_id != fid or artifact.raw_sha256 != row['raw_sha256']
                or m.source_key != doc.source_key or str(m.source_url) != str(doc.source_url)
                or m.title != doc.title or m.issuer != doc.issuer or m.region != doc.region
                or m.published_on != doc.published_on or _utc(m.retrieved_at) != row['observed_at']):
                raise ChunkError('PDF_PROVENANCE_MISMATCH')
        except ChunkError:
            raise
        except Exception as exc:
            raise ChunkError('INVALID_PDF_ARTIFACT') from exc
        pieces = doc.body_text.split(PAGE_BREAK)
        if any(not p.strip() or '\f' in p for p in pieces):
            raise ChunkError('INVALID_PDF_PAGE_BOUNDARY')
        pos = 0
        for number, piece in enumerate(pieces, 1):
            yield number, pos, piece
            pos += len(piece)+len(PAGE_BREAK)
    else:
        if '\f' in doc.body_text or urlsplit(str(doc.source_url)).path.lower().endswith('.pdf'):
            raise ChunkError('PDF_ARTIFACT_REQUIRED')
        yield None, 0, doc.body_text


def build_manifest(db_path, config=ChunkConfig(), *, raw_pdf_dir='storage/raw_pdf'):
    chunks = []
    documents = []
    rows = read_latest(db_path)
    for row in rows:
        if not row['fetch_id'] or not row['raw_sha256']:
            raise ChunkError('MISSING_OBSERVATION')
        doc = PolicyDocument.model_validate({k:row[k] for k in PolicyDocument.model_fields})
        if content_hash(doc) != row['content_sha256']:
            raise ChunkError('CONTENT_HASH_MISMATCH')
        base = dict(policy_id=row['policy_id'], version=row['version'],
            document_id=str(doc.document_id), content_sha256=row['content_sha256'],
            source_key=doc.source_key, source_url=str(doc.source_url), title=doc.title,
            region=doc.region, issuer=doc.issuer,
            published_on=doc.published_on.isoformat() if doc.published_on else None,
            reference_fetch_id=row['fetch_id'], raw_sha256=row['raw_sha256'],
            reference_observed_at=row['observed_at'])
        start_count = len(chunks)
        for page, offset, text in _page_ranges(doc, row, raw_pdf_dir):
            for span in split_spans(text, config):
                part = text[span.start:span.end]
                start, end = offset+span.start, offset+span.end
                if not part.strip():
                    continue  # Only whitespace may be omitted, never content.
                text_sha = hashlib.sha256(part.encode('utf-8')).hexdigest()
                key = dict(algorithm=ALGORITHM, config=asdict(config),
                    document_id=str(doc.document_id), content_sha256=row['content_sha256'],
                    start=start,end=end,page=page,text_sha256=text_sha)
                chunks.append(dict(**base, chunk_id=digest(key), page=page,
                    start=start,end=end,text=part,text_sha256=text_sha))
        documents.append(dict(**base, body_length=len(doc.body_text),
            chunk_count=len(chunks)-start_count))
    data = dict(schema='policy-chunk-manifest-v1', algorithm=ALGORITHM,
        config=asdict(config), selection='latest_observed_only', documents=documents,
        chunks=chunks, notice='Latest observed, not verified legally effective. Character offsets refer to stored normalized text; PDF pages are physical pages when archive verified.')
    return dict(**data, manifest_sha256=digest(data))
