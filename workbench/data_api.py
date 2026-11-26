"""Read-only workbench views and isolated replay over the V2 data layer."""

from contextlib import closing, contextmanager
from dataclasses import asdict
from difflib import SequenceMatcher
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from shutil import rmtree
import sqlite3
from typing import Annotated
from uuid import uuid4

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from powercopy.data.normalizer import ParseError
from powercopy.data.parser import decode_html, load_raw
from powercopy.data.pdf_ingest import PdfIngestError, load_pdf_artifact, preview_pdf
from powercopy.data.pipeline import IngestionPipeline
from powercopy.data.query import BASE, DISCLAIMER, PolicyQueryError, PolicyQueryService
from powercopy.data.sources import build_source_registry
from powercopy.data.versioning import VersionedPolicyStore


ID_PATTERN = r"^[0-9a-f]{32}$"
FileId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Version = Annotated[int, Query(ge=1, le=2147483647)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]

LEDGER = "policies_versioned.db"
LATEST = "(SELECT MAX(x.version) FROM policy_versions x WHERE x.policy_id = i.policy_id)"
MAX_ARCHIVE_JSON = 14 * 1024 * 1024
MAX_REPORT_JSON = 2 * 1024 * 1024
MAX_DIFF_CHARS = 300_000

RUN_FIELDS = ("run_id", "mode", "status", "started_at", "finished_at", "requests")
ITEM_FIELDS = (
    "source_key", "target_key", "stage", "status", "attempts", "fetch_id", "error_type",
    "error_code", "result",
)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetch_ids: list[FileId] = Field(min_length=1, max_length=8)

    @field_validator("fetch_ids")
    @classmethod
    def unique_fetch_ids(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("采集编号不能重复")
        return ids


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


@contextmanager
def _read_ledger(project: Path):
    path = project / "storage" / LEDGER
    if not path.is_file():
        raise _error(503, "POLICY_DB_NOT_FOUND", "版本化政策库暂不可用。")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
    except sqlite3.Error as exc:
        raise _error(503, "POLICY_DB_READ_ERROR", "无法读取版本化政策库。") from exc


def _optional_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_REPORT_JSON + 1)
        if len(raw) > MAX_REPORT_JSON:
            raise ValueError("JSON exceeds size limit")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data
    except (OSError, UnicodeError, ValueError) as exc:
        raise _error(422, "INVALID_DATA_ARTIFACT", "本地 JSON 文件无法读取。") from exc


def _artifact_file(project: Path, folder: str, artifact_id: str, max_bytes: int) -> Path | None:
    if re.fullmatch(ID_PATTERN, artifact_id) is None:
        return None
    try:
        base = (project / "storage" / folder).resolve()
        if not base.is_relative_to(project.resolve()):
            return None
        path = base / f"{artifact_id}.json"
        resolved = path.resolve(strict=True)
        if resolved != path or not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            raise _error(422, "ARTIFACT_TOO_LARGE", "本地文件超出了工作台读取上限。")
        return path
    except OSError:
        return None


def _run_summary(report: dict) -> dict:
    items = report["items"]
    return {
        **{name: report.get(name) for name in RUN_FIELDS},
        "items_total": len(items),
        "succeeded": sum(item.get("status") == "success" for item in items),
        "failed": sum(item.get("status") == "failed" for item in items),
    }


def _run_detail(report: dict) -> dict:
    items = []
    for item in report["items"]:
        entry = {name: item[name] for name in ITEM_FIELDS if name in item}
        if isinstance(item.get("raw_path"), str):
            entry["raw_path"] = item["raw_path"].replace("\\", "/").rsplit("/", 1)[-1]
        items.append(entry)
    return {**_run_summary(report), "items": items}


def _run_report(project: Path, run_id: str) -> dict:
    path = _artifact_file(project, "ingestion_runs", run_id, MAX_REPORT_JSON)
    if path is None:
        raise _error(404, "RUN_NOT_FOUND", "没有找到该采集运行报告。")
    report = _optional_json(path)
    if (
        report.get("run_id") != run_id
        or not isinstance(report.get("items"), list)
        or not all(isinstance(item, dict) for item in report["items"])
    ):
        raise _error(422, "INVALID_RUN_REPORT", "采集运行报告结构异常。")
    return report


def _run_reports(project: Path) -> list[dict]:
    reports = []
    for file in (project / "storage" / "ingestion_runs").glob("*.json"):
        if re.fullmatch(ID_PATTERN, file.stem):
            reports.append(_run_report(project, file.stem))
    return sorted(reports, key=lambda report: report.get("started_at") or "", reverse=True)


def _sources(project: Path) -> dict:
    registry = build_source_registry()
    approvals = _optional_json(project / "config" / "ingestion_approval.json")
    sources = []
    for key in registry.list_sources():
        source = registry.get(key)
        row = asdict(source)
        for target in row["targets"]:
            approval = approvals.get(f"{key}/{target['target_key']}", {})
            target["approved"] = (
                isinstance(approval, dict)
                and approval.get("approved") is True
                and approval.get("url") == target["url"]
                and isinstance(approval.get("evidence"), str)
                and bool(approval["evidence"].strip())
            )
        sources.append(row)

    candidates = {}
    for report in _run_reports(project):
        for item in report["items"]:
            result = item.get("result") or {}
            for candidate in result.get("candidates", []):
                pair = (candidate["source_key"], candidate["url"])
                if candidate.get("status") == "pending_review":
                    candidates.setdefault(pair, candidate)
    return {"items": sources, "candidates": list(candidates.values())}


def overview_data(project_root: Path) -> dict:
    with _read_ledger(project_root) as conn:
        counts = dict(conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM policy_identities) AS policies,
                (SELECT COUNT(*) FROM policy_versions) AS versions,
                (SELECT COUNT(*) FROM policy_fetches) AS fetches,
                (SELECT COUNT(*) FROM policy_documents) AS documents
        """).fetchone())
    counts["historical_versions"] = counts["versions"] - counts["policies"]
    counts["sources"] = len(build_source_registry())
    return {
        "counts": counts,
        "data_source": LEDGER,
        "recent_runs": [_run_summary(report) for report in _run_reports(project_root)[:5]],
    }


def _list_policies(project: Path, keyword: str, region: str, limit: int, offset: int) -> dict:
    filters = f""" WHERE v.version = {LATEST}
        AND (
            ? = '' OR d.region = ?
            OR (? = '__unassigned__' AND (d.region IS NULL OR trim(d.region) = ''))
        )
        AND (? = '' OR instr(d.title, ?) > 0 OR instr(d.body_text, ?) > 0)"""
    params = (region, region, region, keyword, keyword, keyword)
    with _read_ledger(project) as conn:
        total = conn.execute("SELECT COUNT(*) FROM (" + BASE + filters + ")", params).fetchone()[0]
        rows = conn.execute(
            BASE + filters + """ ORDER BY CASE WHEN instr(d.title, ?) > 0 THEN 0 ELSE 1 END,
                d.published_on IS NULL, d.published_on DESC, i.policy_id LIMIT ? OFFSET ?""",
            (*params, keyword, limit, offset),
        ).fetchall()
        groups = conn.execute(
            "SELECT region, COUNT(*) AS total FROM (" + BASE
            + f" WHERE v.version = {LATEST}) GROUP BY region ORDER BY region"
        ).fetchall()
    items = [dict(row, is_latest_observed=bool(row["is_latest_observed"])) for row in rows]
    return {
        "items": items,
        "total": total,
        "regions": [row["region"] for row in groups if (row["region"] or "").strip()],
        "unassigned": sum(row["total"] for row in groups if not (row["region"] or "").strip()),
        "notice": DISCLAIMER,
    }


def _policy_detail(project: Path, policy_id: str, version: int | None, offset: int) -> dict:
    query = PolicyQueryService(project / "storage" / LEDGER)
    try:
        return query.get_policy_detail(policy_id, version, offset)
    except PolicyQueryError as exc:
        if exc.code == "POLICY_VERSION_NOT_FOUND":
            raise _error(404, exc.code, "没有找到该政策版本。") from exc
        if exc.code == "BODY_OFFSET_OUT_OF_RANGE":
            raise _error(422, exc.code, "正文偏移量超出了政策正文。") from exc
        raise _error(503, exc.code, "无法读取版本化政策库。") from exc


def _policy_versions(project: Path, policy_id: str) -> dict:
    with _read_ledger(project) as conn:
        rows = conn.execute(
            BASE + " WHERE i.policy_id = ? ORDER BY v.version DESC", (policy_id,)
        ).fetchall()
    if not rows:
        raise _error(404, "POLICY_NOT_FOUND", "没有找到该政策。")
    return {"items": [dict(row, is_latest_observed=bool(row["is_latest_observed"])) for row in rows]}


def _policy_diff(project: Path, policy_id: str, from_version: int, to_version: int) -> dict:
    with _read_ledger(project) as conn:
        rows = conn.execute("""
            SELECT v.version, d.title, d.issuer, d.region, d.published_on, d.body_text
            FROM policy_versions v JOIN policy_documents d USING (document_id)
            WHERE v.policy_id = ? AND v.version IN (?, ?)
        """, (policy_id, from_version, to_version)).fetchall()
    by_version = {row["version"]: row for row in rows}
    if from_version not in by_version or to_version not in by_version:
        raise _error(404, "POLICY_VERSION_NOT_FOUND", "没有找到至少一个对比版本。")
    before, after = by_version[from_version], by_version[to_version]
    if len(before["body_text"]) + len(after["body_text"]) > MAX_DIFF_CHARS:
        raise _error(422, "DIFF_SIZE_LIMIT", "正文过长，请分别查看两个版本。")
    old = before["body_text"].splitlines(keepends=True)
    new = after["body_text"].splitlines(keepends=True)
    changes = []
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, old, new).get_opcodes():
        if tag in {"delete", "replace"}:
            changes.append({"kind": "delete", "text": "".join(old[old_start:old_end])})
        if tag in {"equal", "insert", "replace"}:
            kind = "equal" if tag == "equal" else "insert"
            changes.append({"kind": kind, "text": "".join(new[new_start:new_end])})
    return {
        "from_version": from_version,
        "to_version": to_version,
        "changes": changes,
        "metadata": [
            {"field": name, "before": before[name], "after": after[name]}
            for name in ("title", "issuer", "region", "published_on")
            if before[name] != after[name]
        ],
    }


def _fetches(project: Path, policy_id: str | None, limit: int, offset: int) -> dict:
    where = " WHERE (? IS NULL OR f.policy_id = ?)"
    with _read_ledger(project) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM policy_fetches f" + where, (policy_id, policy_id)
        ).fetchone()[0]
        rows = conn.execute("""
            SELECT f.*, v.document_id, v.content_sha256, d.title, d.source_key, d.source_url
            FROM policy_fetches f JOIN policy_versions v USING (policy_id, version)
            JOIN policy_documents d USING (document_id)
        """ + where + " ORDER BY f.retrieved_at DESC, f.fetch_id DESC LIMIT ? OFFSET ?",
            (policy_id, policy_id, limit, offset),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total}


def _raw_archive(project: Path, fetch_id: str) -> tuple[dict, object, Path]:
    html = _artifact_file(project, "raw", fetch_id, MAX_ARCHIVE_JSON)
    pdf = _artifact_file(project, "raw_pdf", fetch_id, MAX_ARCHIVE_JSON)
    if html is None and pdf is None:
        raise _error(404, "ARCHIVE_NOT_FOUND", "没有找到该原始档案。")
    if html and pdf:
        raise _error(422, "ARCHIVE_ID_CONFLICT", "原始档案编号冲突。")
    try:
        if html:
            raw = load_raw(html)
            digest = sha256(raw.body_bytes()).hexdigest()
            if raw.fetch_id != fetch_id:
                raise ValueError("filename and artifact id differ")
            registry = build_source_registry()
            try:
                source, target = registry.resolve(raw.source_key, raw.target_key)
                registered = target.url == str(raw.source_url) and target.content_kind == raw.content_kind
            except KeyError:
                registered = False
                source = None
            return {
                **raw.model_dump(mode="json", exclude={"body_base64"}),
                "filename": html.name,
                "raw_sha256": digest,
                "preview_kind": "html",
                "replayable": bool(source and source.enabled and registered),
                "provenance": "official_capture" if registered else "unverified_archive",
            }, raw, html
        artifact = load_pdf_artifact(pdf)
        if artifact.fetch_id != fetch_id:
            raise ValueError("filename and artifact id differ")
        metadata = artifact.metadata.model_dump(mode="json")
        return {
            **metadata,
            "fetch_id": fetch_id,
            "filename": pdf.name,
            "acquisition_kind": artifact.acquisition_kind,
            "content_kind": "pdf",
            "content_type": "application/pdf",
            "body_size": artifact.body_size,
            "raw_sha256": artifact.raw_sha256,
            "preview_kind": "pdf",
            "replayable": False,
            "provenance": (
                "synthetic_fixture"
                if metadata["target_key"] == "synthetic_fixture" else "manual_offline"
            ),
        }, artifact, pdf
    except (ParseError, PdfIngestError, OSError, ValueError) as exc:
        raise _error(422, "INVALID_RAW_ARTIFACT", "原始档案格式或完整性校验失败。") from exc


def _archives(project: Path) -> dict:
    ids = {
        file.stem
        for folder in ("raw", "raw_pdf")
        for file in (project / "storage" / folder).glob("*.json")
        if re.fullmatch(ID_PATTERN, file.stem)
    }
    items = []
    unavailable = []
    for fetch_id in sorted(ids):
        try:
            items.append(_raw_archive(project, fetch_id)[0])
        except HTTPException as exc:
            unavailable.append({"fetch_id": fetch_id, **exc.detail})
    items.sort(key=lambda item: item["retrieved_at"], reverse=True)
    return {"items": items, "unavailable": unavailable}


def _preview(project: Path, fetch_id: str) -> HTMLResponse:
    metadata, archive, _ = _raw_archive(project, fetch_id)
    try:
        if metadata["preview_kind"] == "pdf":
            pages = preview_pdf(archive).pages
            content = "".join(
                f"<section><h2>第 {page.number} 页</h2><pre>{escape(page.text)}</pre></section>"
                for page in pages
            )
        else:
            decoded, _ = decode_html(archive.body_bytes(), archive.charset)
            soup = BeautifulSoup(decoded, "html.parser")
            for node in soup.select("script, style, noscript, template, iframe, object, svg"):
                node.decompose()
            content = f"<pre>{escape(soup.get_text(chr(10), strip=True))}</pre>"
    except (ParseError, PdfIngestError) as exc:
        raise _error(422, "ARCHIVE_PREVIEW_FAILED", "该档案暂时无法生成文本预览。") from exc

    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<title>原始档案预览</title><meta name="viewport" content="width=device-width">'
        "<style>body{font:15px/1.75 system-ui;color:#ecf5f7;background:#081217;margin:20px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}"
        "h2{font-size:14px;color:#84eaff}section{padding:12px 0;border-bottom:1px solid #233740}"
        "</style></head><body>" + content + "</body></html>",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _replay(project: Path, fetch_ids: list[str]) -> dict:
    archives = [_raw_archive(project, fetch_id) for fetch_id in fetch_ids]
    if any(not metadata["replayable"] for metadata, _, _ in archives):
        raise _error(422, "ARCHIVE_NOT_REPLAYABLE", "仅支持已经注册的 HTML 采集档案离线重放。")
    paths = [path for _, _, path in sorted(archives, key=lambda item: item[1].retrieved_at)]
    profiles = _optional_json(project / "config" / "parser_profiles.json")
    selectors = _optional_json(project / "config" / "index_selectors.json")
    workspace = Path(__file__).resolve().parents[1]
    scratch = (workspace / "workspace").resolve()
    if scratch.parent != workspace:
        raise _error(503, "REPLAY_STORAGE_UNAVAILABLE", "无法使用工作台的离线重放目录。")
    root = scratch / f"replay-{uuid4().hex}"
    try:
        # Python's private tempfile directory mode can deny this process access on Windows.
        root.mkdir(parents=True)
    except OSError as exc:
        raise _error(503, "REPLAY_STORAGE_UNAVAILABLE", "无法创建离线重放目录。") from exc
    try:
        store = VersionedPolicyStore(root / LEDGER)
        pipeline = IngestionPipeline(store, profiles, raw_dir=root / "raw", run_dir=root / "runs")
        report = pipeline.replay(paths, selectors)
        return {**_run_detail(report), "isolated": True, "counts": store.counts()}
    except (OSError, sqlite3.Error) as exc:
        raise _error(503, "REPLAY_STORAGE_UNAVAILABLE", "无法完成离线重放。") from exc
    finally:
        if root.resolve() == root and root.parent == scratch:
            rmtree(root)


def build_data_router(project_root: Path) -> APIRouter:
    router = APIRouter()
    project = project_root.resolve()

    @router.get("/sources")
    def sources():
        return {"data": _sources(project)}

    @router.get("/ingestion/runs")
    def ingestion_runs():
        return {"data": {"items": [_run_summary(report) for report in _run_reports(project)]}}

    @router.get("/ingestion/runs/{run_id}")
    def ingestion_run(run_id: FileId):
        return {"data": _run_detail(_run_report(project, run_id))}

    @router.post("/ingestion/replay")
    def ingestion_replay(body: ReplayRequest):
        return {"data": _replay(project, body.fetch_ids)}

    @router.get("/policies")
    def policies(
        keyword: Annotated[str, Query(max_length=200)] = "",
        region: Annotated[str, Query(max_length=80)] = "",
        limit: Limit = 20,
        offset: Offset = 0,
    ):
        return {"data": _list_policies(project, keyword.strip(), region.strip(), limit, offset)}

    @router.get("/policies/{policy_id}")
    def policy_detail(policy_id: FileId, version: Version | None = None, offset: Offset = 0):
        return {"data": _policy_detail(project, policy_id, version, offset)}

    @router.get("/policies/{policy_id}/versions")
    def policy_versions(policy_id: FileId):
        return {"data": _policy_versions(project, policy_id)}

    @router.get("/policies/{policy_id}/diff")
    def policy_diff(policy_id: FileId, from_version: Version, to_version: Version):
        return {"data": _policy_diff(project, policy_id, from_version, to_version)}

    @router.get("/fetches")
    def fetches(policy_id: FileId | None = None, limit: Limit = 20, offset: Offset = 0):
        return {"data": _fetches(project, policy_id, limit, offset)}

    @router.get("/archive")
    def archive():
        return {"data": _archives(project)}

    @router.get("/archive/{fetch_id}")
    def archive_detail(fetch_id: FileId):
        return {"data": _raw_archive(project, fetch_id)[0]}

    @router.get("/archive/{fetch_id}/preview", response_class=HTMLResponse)
    def archive_preview(fetch_id: FileId):
        return _preview(project, fetch_id)

    return router
