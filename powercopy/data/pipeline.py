"""4.5: sequential ingestion. No dynamic network targets, no LLM."""
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from powercopy.data.parser import RawPage, load_raw, decode_html, ParseProfile


class PipelineError(Exception):
    pass


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def key(raw):
    return f'{raw.source_key}/{raw.target_key}'


def discover(raw, selector, limit=20):
    """Offline review candidates only; selector must be calibrated by operator."""
    if raw.content_kind != 'index' or raw.status_code != 200:
        raise PipelineError('NOT_AN_INDEX')
    if not 1 <= limit <= 100:
        raise ValueError('invalid candidate limit')
    html, _ = decode_html(raw.body_bytes(), raw.charset)
    soup = BeautifulSoup(html, 'html.parser')
    base = str(raw.source_url)
    host = urlsplit(base).hostname
    seen, found = set(), []
    matches = soup.select(selector)
    if not matches:
        raise PipelineError('INDEX_SELECTOR_NO_MATCH')
    for tag in matches:
        href = tag.get('href')
        title = tag.get_text(' ', strip=True)
        if tag.name != 'a' or not href or not title:
            continue
        try:
            url = urljoin(base, href)
            u = urlsplit(url)
            if (u.scheme != 'https' or u.hostname != host or u.username is not None
                or u.password is not None or u.port not in (None, 443) or u.fragment):
                continue
        except ValueError:
            continue
        if url == base or url in seen:
            continue
        seen.add(url)
        found.append(dict(source_key=raw.source_key, index_fetch_id=raw.fetch_id,
                          url=url, title=title, status='pending_review'))
        if len(found) >= limit:
            break
    return found


def save_fetch(result, directory):
    # Structural adapter for the verified FetchResult field contract.
    data = asdict(result)
    body = data.pop('body')
    fid = uuid4().hex
    data.update(fetch_id=fid, body_size=len(body),
                body_base64=base64.b64encode(body).decode('ascii'),
                retrieved_at=result.retrieved_at.isoformat())
    raw = RawPage.model_validate(data)
    raw.body_bytes()
    path = Path(directory) / (fid + '.json')
    atomic_json(path, data)
    return path


class IngestionPipeline:
    def __init__(self, store, profiles, *, raw_dir, run_dir, fetcher=None,
                 registry=None, approvals=None, max_requests=8, max_attempts=2,
                 interval=2.0, backoff=2.0, clock=monotonic, sleeper=sleep):
        if not (1 <= max_requests <= 100 and 1 <= max_attempts <= 3
                and interval >= 2 and backoff >= 1):
            raise ValueError('invalid pipeline limits')
        self.store, self.profiles = store, profiles
        self.raw_dir, self.run_dir = Path(raw_dir), Path(run_dir)
        self.fetcher, self.registry = fetcher, registry
        self.approvals = approvals or {}
        self.max_requests, self.max_attempts = max_requests, max_attempts
        self.interval, self.backoff = interval, backoff
        self.clock, self.sleeper = clock, sleeper

    def _new_report(self, mode):
        return dict(run_id=uuid4().hex, mode=mode, status='running',
                    started_at=datetime.now(timezone.utc).isoformat(), requests=0, items=[])

    def _save(self, report):
        atomic_json(self.run_dir / (report['run_id']+'.json'), report)

    def _finish(self, report):
        failures = sum(x['status'] != 'success' for x in report['items'])
        report['status'] = ('success' if not failures else
                            'failed' if failures == len(report['items']) else 'partial')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        self._save(report)
        return report

    def _process(self, raw, item, index_selectors):
        item['stage'] = 'process'
        item['fetch_id'] = raw.fetch_id
        if raw.content_kind == 'policy':
            profile = ParseProfile.model_validate(self.profiles[key(raw)])
            item['result'] = asdict(self.store.ingest(raw, profile))
        elif raw.content_kind == 'index':
            item['result'] = dict(action='discovered', candidates=discover(raw, index_selectors[key(raw)]))
        elif raw.content_kind == 'market_news':
            item['result'] = dict(action='archived_only')
        else:
            raise PipelineError('UNSUPPORTED_KIND')
        item['status'] = 'success'

    @staticmethod
    def _failed(item, exc):
        item['status'] = 'failed'
        item['error_type'] = type(exc).__name__
        # No raw error strings, HTML, or credentials in reports.
        code = getattr(exc, 'code', None)
        if isinstance(code, str):
            item['error_code'] = code
        elif isinstance(exc, PipelineError):
            item['error_code'] = str(exc)
        elif type(exc).__name__ in {'ParseError', 'VersionError'}:
            item['error_code'] = str(exc).split(':', 1)[0]

    def replay(self, paths, index_selectors=None):
        paths = list(paths)
        if not paths:
            raise ValueError('no inputs')
        report = self._new_report('offline')
        self._save(report)
        # Caller must provide chronological input; late events fail closed in store.
        for path in paths:
            item = dict(raw_path=str(path), stage='load_raw', status='running', attempts=[])
            report['items'].append(item)
            self._save(report)
            try:
                self._process(load_raw(path), item, index_selectors or {})
            except Exception as exc:
                self._failed(item, exc)
            self._save(report)
        return self._finish(report)

    def live(self, targets, index_selectors=None):
        targets = list(targets)
        if not targets or not self.fetcher or not self.registry:
            raise ValueError('live configuration required')
        report = self._new_report('live')
        self._save(report)
        last_start = None
        paused = False
        for source, target in targets:
            item = dict(source_key=source, target_key=target, stage='authorize',
                        status='running', attempts=[])
            report['items'].append(item)
            self._save(report)
            try:
                if paused:
                    raise PipelineError('RUN_PAUSED')
                _, registered = self.registry.resolve(source, target)
                k = f'{source}/{target}'
                approval = self.approvals.get(k, {})
                if (approval.get('url') != registered.url or
                    approval.get('approved') is not True or not isinstance(approval.get('evidence'), str) or not approval['evidence'].strip()):
                    raise PipelineError('NO_OPERATOR_APPROVAL')
                if registered.content_kind == 'policy':
                    ParseProfile.model_validate(self.profiles[k])
                elif registered.content_kind == 'index' and not (index_selectors or {}).get(k):
                    raise PipelineError('MISSING_INDEX_SELECTOR')
                item['stage'] = 'fetch'
                for i in range(self.max_attempts):
                    if report['requests'] >= self.max_requests:
                        raise PipelineError('REQUEST_BUDGET_EXHAUSTED')
                    if last_start is not None:
                        self.sleeper(max(0, self.interval-(self.clock()-last_start)))
                    last_start = self.clock()
                    report['requests'] += 1
                    attempt = dict(number=i+1, status='started')
                    item['attempts'].append(attempt)
                    self._save(report)  # write-ahead attempt intent
                    try:
                        result = self.fetcher.fetch(source, target)
                    except Exception as exc:
                        code = str(getattr(exc, 'code', '')).lower()
                        status = getattr(exc, 'status_code', None)
                        attempt.update(status='failed', error_type=type(exc).__name__, code=code, http_status=status)
                        self._save(report)
                        if status in (403, 429) or code in ('http_403','http_429'):
                            paused = True
                        transient = (status in (500,502,503,504) or code in
                                     ('http_500','http_502','http_503','http_504','network_error','timeout'))
                        if paused or not transient or i+1 >= self.max_attempts or report['requests'] >= self.max_requests:
                            raise
                        self.sleeper(self.backoff * (2**i))
                    else:
                        attempt['status'] = 'success'
                        self._save(report)
                        break
                if (result.source_key != source or result.target_key != target or
                    result.source_url != registered.url or result.content_kind != registered.content_kind or
                    result.status_code != 200):
                    raise PipelineError('FETCH_RESULT_MISMATCH')
                item['stage'] = 'save_raw'
                path = save_fetch(result, self.raw_dir)
                item['raw_path'] = str(path)
                self._save(report)
                self._process(load_raw(path), item, index_selectors or {})
            except Exception as exc:
                self._failed(item, exc)
            self._save(report)
        return self._finish(report)
