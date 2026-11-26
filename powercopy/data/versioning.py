"""4.4: atomic versioned ingest. Only this writer may write the versioned DB."""
from contextlib import closing
from dataclasses import dataclass
from datetime import timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from powercopy.data.models import PolicyDocument
from powercopy.data.parser import RawPage, ParseProfile, parse_article, to_policy_document
from powercopy.data.repository import PolicyRepository


class VersionError(Exception):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value: dict) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':')).encode('utf-8'))


def content_hash(document: PolicyDocument) -> str:
    # Only business fields; no UUID, crawl time or HTML layout.
    data = document.model_dump(mode='json', include={
        'title', 'body_text', 'issuer', 'region', 'published_on'})
    return _json_hash({'schema': 'policy-content-v1', **data})


def _utc(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class IngestResult:
    action: str
    policy_id: str
    document_id: str
    version: int
    fetch_id: str
    content_sha256: str


DDL = (
    '''CREATE TABLE IF NOT EXISTS policy_identities (
        policy_id TEXT PRIMARY KEY NOT NULL,
        source_key TEXT NOT NULL, source_url TEXT NOT NULL,
        UNIQUE(source_key, source_url))''',
    '''CREATE TABLE IF NOT EXISTS policy_versions (
        policy_id TEXT NOT NULL REFERENCES policy_identities(policy_id),
        version INTEGER NOT NULL CHECK(version > 0),
        document_id TEXT NOT NULL UNIQUE REFERENCES policy_documents(document_id),
        content_sha256 TEXT NOT NULL,
        PRIMARY KEY(policy_id, version))''',
    '''CREATE TABLE IF NOT EXISTS policy_fetches (
        fetch_id TEXT PRIMARY KEY NOT NULL,
        policy_id TEXT NOT NULL, version INTEGER NOT NULL,
        target_key TEXT NOT NULL, retrieved_at TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL, input_sha256 TEXT NOT NULL,
        FOREIGN KEY(policy_id, version) REFERENCES policy_versions(policy_id, version))''',
    '''CREATE INDEX IF NOT EXISTS idx_policy_fetches_time
       ON policy_fetches(policy_id, retrieved_at)''',
)

INSERT_DOCUMENT = '''INSERT INTO policy_documents (
    document_id, source_key, source_url, title, body_text, region,
    issuer, published_on, retrieved_at) VALUES (
    :document_id, :source_key, :source_url, :title, :body_text, :region,
    :issuer, :published_on, :retrieved_at)'''


class VersionedPolicyStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        # Existing repository initializes the unchanged document table.
        PolicyRepository(self.db_path)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            for sql in DDL:
                conn.execute(sql)
            self._check_untracked(conn)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    @staticmethod
    def _check_untracked(conn):
        if conn.execute('''SELECT 1 FROM policy_documents d
            WHERE NOT EXISTS (SELECT 1 FROM policy_versions v
            WHERE v.document_id=d.document_id) LIMIT 1''').fetchone():
            raise VersionError('UNTRACKED_DOCUMENTS_USE_NEW_DB_OR_MIGRATE')

    def ingest(self, raw: RawPage, profile: ParseProfile) -> IngestResult:
        # No LLM/network. Parsing is outside the write transaction.
        if not raw.fetch_id.strip() or not raw.target_key.strip():
            raise VersionError('INVALID_FETCH_ID_OR_TARGET')
        if raw.content_type.split(';', 1)[0].strip().lower() not in {
            'text/html', 'application/xhtml+xml'}:
            raise VersionError('UNSUPPORTED_CONTENT_TYPE')
        article = parse_article(raw, profile)
        doc = to_policy_document(raw, article)
        raw_sha = _sha(raw.body_bytes())
        digest = content_hash(doc)
        stamp = _utc(raw.retrieved_at)
        source_key, url = doc.source_key, str(doc.source_url)
        fingerprint = _json_hash({
            'source_key': source_key, 'source_url': url,
            'target_key': raw.target_key, 'retrieved_at': stamp,
            'raw_sha256': raw_sha, 'content_sha256': digest,
            'content_kind': raw.content_kind, 'status_code': raw.status_code,
            'content_type': raw.content_type, 'charset': raw.charset,
            'profile': profile.model_dump(mode='json'),
        })
        return self._ingest_prepared(
            doc, fetch_id=raw.fetch_id, target_key=raw.target_key,
            stamp=stamp, raw_sha=raw_sha, digest=digest, fingerprint=fingerprint,
        )

    def _ingest_prepared(self, doc: PolicyDocument, *, fetch_id: str,
                         target_key: str, stamp: str, raw_sha: str,
                         digest: str, fingerprint: str) -> IngestResult:
        """公共事务写入入口：HTML 与 PDF 共用同一套版本化写入逻辑。

        这是应用内部受信任的适配边界，**不是** Agent 工具，
        模型无法控制 fetch_id、target_key、原始哈希或输入指纹。
        """
        if not fetch_id.strip() or not target_key.strip():
            raise VersionError('INVALID_FETCH_ID_OR_TARGET')
        source_key, url = doc.source_key, str(doc.source_url)
        with closing(self._connect()) as conn, conn:
            # Acquire write lock before reading latest version; all writes atomic.
            conn.execute('BEGIN IMMEDIATE')
            self._check_untracked(conn)
            old = conn.execute('''SELECT f.*, v.document_id, v.content_sha256
                FROM policy_fetches f JOIN policy_versions v
                USING(policy_id, version) WHERE f.fetch_id=?''', (fetch_id,)).fetchone()
            if old:
                if old['input_sha256'] != fingerprint:
                    raise VersionError('FETCH_ID_CONFLICT')
                return IngestResult('replayed', old['policy_id'], old['document_id'],
                                    old['version'], fetch_id, old['content_sha256'])
            identity = conn.execute('''SELECT policy_id FROM policy_identities
                WHERE source_key=? AND source_url=?''', (source_key, url)).fetchone()
            if identity is None:
                pid = uuid4().hex
                conn.execute('INSERT INTO policy_identities VALUES (?,?,?)',
                             (pid, source_key, url))
                latest = None
            else:
                pid = identity['policy_id']
                latest = conn.execute('''SELECT * FROM policy_versions
                    WHERE policy_id=? ORDER BY version DESC LIMIT 1''', (pid,)).fetchone()
                last = conn.execute('''SELECT MAX(retrieved_at) AS stamp
                    FROM policy_fetches WHERE policy_id=?''', (pid,)).fetchone()['stamp']
                if latest is None or last is None:
                    raise VersionError('INCONSISTENT_LEDGER')
                if stamp < last:
                    raise VersionError('OUT_OF_ORDER_FETCH')
                if stamp == last and digest != latest['content_sha256']:
                    raise VersionError('AMBIGUOUS_FETCH_ORDER')
            if latest is not None and latest['content_sha256'] == digest:
                version, did = latest['version'], latest['document_id']
                action = 'unchanged'
            else:
                version = 1 if latest is None else latest['version'] + 1
                did = str(doc.document_id)
                action = 'created' if latest is None else 'updated'
                # Do NOT call repo.add(): that would commit on another connection.
                conn.execute(INSERT_DOCUMENT, doc.model_dump(mode='json'))
                conn.execute('INSERT INTO policy_versions VALUES (?,?,?,?)',
                             (pid, version, did, digest))
            conn.execute('INSERT INTO policy_fetches VALUES (?,?,?,?,?,?,?)',
                         (fetch_id, pid, version, target_key, stamp, raw_sha, fingerprint))
            return IngestResult(action, pid, did, version, fetch_id, digest)

    def history(self, policy_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute('''SELECT v.*,
                (SELECT COUNT(*) FROM policy_fetches f WHERE f.policy_id=v.policy_id
                 AND f.version=v.version) AS observations
                FROM policy_versions v WHERE v.policy_id=? ORDER BY version''', (policy_id,))]

    def observations(self, policy_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute('''SELECT * FROM policy_fetches
                WHERE policy_id=? ORDER BY retrieved_at, fetch_id''', (policy_id,))]

    def counts(self) -> dict:
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            return {key: conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                    for key, table in [('policies','policy_identities'),
                    ('versions','policy_versions'), ('fetches','policy_fetches'),
                    ('documents','policy_documents')]}
