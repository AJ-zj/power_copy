"""Read-only, literal policy search over the 4.4 versioned ledger."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

class PolicyQueryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class SearchPolicyArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    region: str = Field(max_length=80, description='Exact stored region; empty means all regions. Unassigned region is included but not proof of applicability.')
    keyword: str = Field(min_length=1, max_length=200, description='Literal substring of title or full policy text, not semantic search.')
    limit: int = Field(default=5, ge=1, le=10)

class PolicyDetailArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    policy_id: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{32}$')]
    version: int | None = Field(default=None, ge=1, le=2147483647, description='Omit for latest observed content version, not legal validity.')
    offset: int = Field(default=0, ge=0, le=10000000)

# One result per policy version, with latest observation of that SAME version.
BASE = '''SELECT i.policy_id, v.version, v.document_id, v.content_sha256,
 d.title, d.region, d.issuer, d.published_on, d.source_key, d.source_url,
 d.retrieved_at AS document_retrieved_at, f.fetch_id, f.raw_sha256,
 f.retrieved_at AS last_observed_at,
 (SELECT COUNT(*) FROM policy_fetches x WHERE x.policy_id=v.policy_id AND x.version=v.version) AS observation_count,
 (v.version=(SELECT MAX(y.version) FROM policy_versions y WHERE y.policy_id=i.policy_id)) AS is_latest_observed,
 length(d.body_text) AS body_length
 FROM policy_identities i JOIN policy_versions v ON v.policy_id=i.policy_id
 JOIN policy_documents d ON d.document_id=v.document_id
 JOIN policy_fetches f ON f.fetch_id=(SELECT x.fetch_id FROM policy_fetches x
 WHERE x.policy_id=v.policy_id AND x.version=v.version ORDER BY x.retrieved_at DESC,x.fetch_id DESC LIMIT 1)'''

DISCLAIMER = '仅代表本地库中最近观测到的版本，未核验当前法律效力、适用范围及网站最新状态。'

class PolicyQueryService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()

    @contextmanager
    def _read(self):
        if not self.db_path.is_file():
            raise PolicyQueryError('POLICY_DB_NOT_FOUND')
        try:
            with closing(sqlite3.connect(self.db_path.as_uri()+'?mode=ro', uri=True, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                yield conn
        except sqlite3.Error as exc:
            raise PolicyQueryError('POLICY_DB_READ_ERROR') from exc

    def search_policy(self, region: str, keyword: str, limit: int = 5) -> dict:
        args = SearchPolicyArgs(region=region, keyword=keyword, limit=limit)
        # Literal substring, not LIKE pattern matching; % and _ remain literal.
        sql = BASE + ''' WHERE v.version=(SELECT MAX(y.version) FROM policy_versions y WHERE y.policy_id=i.policy_id)
          AND (?='' OR d.region=? OR d.region IS NULL OR trim(d.region)='')
          AND (instr(d.title,?)>0 OR instr(d.body_text,?)>0)
          ORDER BY CASE WHEN instr(d.title,?)>0 THEN 0 ELSE 1 END,
          CASE WHEN d.region=? THEN 0 ELSE 1 END,
          d.published_on IS NULL, d.published_on DESC, i.policy_id LIMIT ?'''
        with self._read() as conn:
            rows = conn.execute(sql,(args.region,args.region,args.keyword,args.keyword,args.keyword,args.region,args.limit+1)).fetchall()
        items=[]
        for row in rows[:args.limit]:
            item=dict(row)
            item['is_latest_observed']=bool(item['is_latest_observed'])
            item['region_match']='not_filtered' if not args.region else ('exact' if item['region']==args.region else 'unassigned')
            items.append(item)
        return {'items':items,'returned':len(items),'has_more':len(rows)>args.limit,
                'search_mode':'literal_substring','region_filter':'exact_or_unassigned' if args.region else 'all',
                'notice':DISCLAIMER+' 地区未标注不代表全国适用；未命中不代表不存在政策。'}

    def get_policy_detail(self, policy_id: str, version: int | None = None, offset: int = 0) -> dict:
        args=PolicyDetailArgs(policy_id=policy_id,version=version,offset=offset)
        # Keep a single read transaction for metadata + body slice.
        with self._read() as conn:
            row=conn.execute(BASE+''' WHERE i.policy_id=? AND v.version=COALESCE(?,(SELECT MAX(y.version) FROM policy_versions y WHERE y.policy_id=i.policy_id))''',(args.policy_id,args.version)).fetchone()
            if row is None:
                raise PolicyQueryError('POLICY_VERSION_NOT_FOUND')
            item=dict(row)
            if args.offset >= item['body_length']:
                raise PolicyQueryError('BODY_OFFSET_OUT_OF_RANGE')
            body=conn.execute('SELECT substr(body_text,?,?) FROM policy_documents WHERE document_id=?',(args.offset+1,8000,item['document_id'])).fetchone()[0]
        end=args.offset+len(body)
        item.update(is_latest_observed=bool(item['is_latest_observed']),body_text=body,body_offset=args.offset,
                    next_offset=end if end<item['body_length'] else None, body_complete=args.offset==0 and end==item['body_length'],notice=DISCLAIMER)
        return item
