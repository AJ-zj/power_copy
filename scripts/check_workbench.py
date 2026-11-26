"""Read-only integration checks for a running PowerCopy workbench API."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


class CheckFailure(AssertionError):
    pass


class Client:
    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("--base-url must be an absolute HTTP URL")
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                content_type = response.headers.get_content_type()
                data = json.loads(raw) if content_type == "application/json" else raw
                return response.status, data, response.headers
        except HTTPError as exc:
            with exc:
                raw = exc.read()
                content_type = exc.headers.get_content_type()
                data = json.loads(raw) if raw and content_type == "application/json" else raw
                return exc.code, data, exc.headers

    def get(self, path: str) -> dict:
        status, payload, _ = self.request("GET", path)
        expect(status == 200, f"GET {path}: expected 200, received {status}")
        expect(isinstance(payload, dict) and "data" in payload, f"GET {path}: success envelope missing")
        return payload["data"]


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def project_counts(db_path: Path) -> dict:
    if not db_path.is_file():
        raise CheckFailure(f"versioned policy database not found: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return dict(connection.execute("""
            SELECT
                (SELECT COUNT(*) FROM policy_identities) AS policies,
                (SELECT COUNT(*) FROM policy_versions) AS versions,
                (SELECT COUNT(*) FROM policy_fetches) AS fetches,
                (SELECT COUNT(*) FROM policy_documents) AS documents
        """).fetchone())


def database_hash(db_path: Path) -> str:
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


def run_checks(base_url: str, project_root: Path) -> dict:
    client = Client(base_url)
    project = project_root.resolve()
    db_path = project / "storage" / "policies_versioned.db"
    sql_counts = project_counts(db_path)
    before_hash = database_hash(db_path)
    passed: list[str] = []

    overview = client.get("/api/workbench/overview")
    counts = overview["counts"]
    for field, value in sql_counts.items():
        expect(counts[field] == value, f"overview {field} differs from SQLite")
    expect(counts["historical_versions"] == sql_counts["versions"] - sql_counts["policies"],
           "overview historical version count is inconsistent")
    passed.append("Overview counts match read-only SQLite")

    sources = client.get("/api/workbench/sources")
    expect(len(sources["items"]) == counts["sources"], "source registry count differs from overview")
    for source in sources["items"]:
        for target in source["targets"]:
            parsed = urlsplit(target["url"])
            expect(parsed.scheme == "https" and parsed.hostname == source["allowed_host"],
                   f"unregistered or unsafe target in {source['source_key']}")
    candidates = sources["candidates"]
    expect(all(item["status"] == "pending_review" for item in candidates),
           "an ingestion candidate was presented as approved")
    passed.append("Registered targets and pending candidates match their provenance")

    policies = client.get("/api/workbench/policies?limit=100")
    expect(policies["total"] == sql_counts["policies"], "policy list count differs from SQLite")
    selected_policy = None
    for item in policies["items"]:
        detail = client.get("/api/workbench/policies/" + quote(item["policy_id"]))
        expect(detail["policy_id"] == item["policy_id"], "policy detail id mismatch")
        expect(detail["content_sha256"] == item["content_sha256"],
               f"policy hash mismatch for {item['policy_id']}")
        expect(detail["body_text"] and detail["body_length"] >= len(detail["body_text"]),
               "policy detail body slice is missing")
        expect(detail["is_latest_observed"], "latest policy detail is marked stale")
        selected_policy = selected_policy or (item, detail)
        if item["region"] is None:
            unassigned = client.get("/api/workbench/policies?" + urlencode({
                "region": "__unassigned__", "limit": 100,
            }))
            expect(any(row["policy_id"] == item["policy_id"] for row in unassigned["items"]),
                   "unassigned region filter omitted a policy")
        title_keyword = item["title"][: min(6, len(item["title"]))]
        search = client.get("/api/workbench/policies?" + urlencode({
            "keyword": title_keyword, "region": item["region"] or "", "limit": 100,
        }))
        expect(any(row["policy_id"] == item["policy_id"] for row in search["items"]),
               "literal title keyword search omitted its policy")
    expect(selected_policy is not None, "real policy library is empty")

    literal = client.get("/api/workbench/policies?" + urlencode({"keyword": "%_%", "limit": 100}))
    expect(literal["total"] == 0, "wildcards were interpreted instead of searched literally")
    passed.append("Policy details, literal keyword search, and region filtering match SQLite")

    item, _ = selected_policy
    history = client.get("/api/workbench/policies/" + quote(item["policy_id"]) + "/versions")
    expect(history["items"], "policy version history is empty")
    expect(any(row["version"] == item["version"] for row in history["items"]),
           "latest version is missing from its timeline")
    genuine_version = min(row["version"] for row in history["items"])
    diff = client.get("/api/workbench/policies/" + quote(item["policy_id"])
                      + "/diff?" + urlencode({
                          "from_version": genuine_version, "to_version": genuine_version,
                      }))
    expect(diff["from_version"] == genuine_version and diff["to_version"] == genuine_version,
           "same-version policy diff does not resolve a genuine stored version")
    expect(all(change["kind"] == "equal" for change in diff["changes"]),
           "same-version policy diff unexpectedly contains changes")
    passed.append("Stored version timeline and diff resolve real version data")

    fetches = client.get("/api/workbench/fetches?limit=100")
    expect(fetches["total"] == sql_counts["fetches"], "fetch count differs from SQLite")
    expect(fetches["items"], "no real fetch records are available")
    run_list = client.get("/api/workbench/ingestion/runs")["items"]
    expect(run_list and run_list[:5] == overview["recent_runs"],
           "saved ingestion reports are absent from overview")
    for report in run_list:
        detail = client.get("/api/workbench/ingestion/runs/" + quote(report["run_id"]))
        expect(detail["run_id"] == report["run_id"], "ingestion report id mismatch")
        expect(detail["items_total"] == report["items_total"], "ingestion report item count mismatch")
        if detail["mode"] == "offline":
            expect(detail["requests"] == 0, "offline ingestion report claims network requests")
    passed.append("Fetch records and saved ingestion reports are readable")

    archive = client.get("/api/workbench/archive")
    expect(archive["items"], "no raw archives are available")
    policy_archives = [row for row in archive["items"]
                       if row["content_kind"] == "policy" and row["replayable"]]
    expect(policy_archives, "no registered HTML archive supports offline replay")
    for row in archive["items"]:
        metadata = client.get("/api/workbench/archive/" + quote(row["fetch_id"]))
        expect(metadata["raw_sha256"] == row["raw_sha256"],
               f"raw archive hash mismatch for {row['fetch_id']}")
        status, body, headers = client.request(
            "GET", "/api/workbench/archive/" + quote(row["fetch_id"]) + "/preview"
        )
        expect(status == 200 and isinstance(body, bytes), "raw archive preview failed")
        expect("sandbox" in headers.get("Content-Security-Policy", ""),
               "raw preview lacks a restrictive sandbox policy")
        expect(b"<script" not in body.lower(), "raw preview contains executable script markup")
    passed.append("Raw archive metadata, hashes, and safe previews are available")

    pre_replay_hash = database_hash(db_path)
    replay = client.request("POST", "/api/workbench/ingestion/replay", {
        "fetch_ids": [policy_archives[0]["fetch_id"]],
    })
    expect(replay[0] == 200 and replay[1]["data"]["isolated"],
           f"controlled offline replay failed: HTTP {replay[0]}")
    replay_data = replay[1]["data"]
    expect(replay_data["requests"] == 0, "offline replay made a network request")
    expect(replay_data["status"] == "success", "offline replay did not process a valid archive")
    expect(replay_data["counts"]["documents"] >= 1, "offline replay did not parse a policy")
    expect(database_hash(db_path) == pre_replay_hash, "offline replay changed the real policy database")
    passed.append("Offline replay processed a real archive in isolation with zero network requests")

    tools = client.get("/api/workbench/tools")["items"]
    tool_names = {tool["name"] for tool in tools}
    expect({"search_policy", "get_policy_detail"} <= tool_names,
           "runtime did not expose the two actual read-only policy tools")
    request = {
        "task": "在本地政策库中核对政策依据。",
        "keyword": selected_policy[0]["title"][: min(6, len(selected_policy[0]["title"]))],
        "region": selected_policy[0]["region"] or "",
        "mode": "local",
    }
    status, run_envelope, _ = client.request("POST", "/api/workbench/runs", request)
    expect(status == 200 and "data" in run_envelope, f"local Agent run failed: HTTP {status}")
    execution = run_envelope["data"]
    expect(execution["mode"] == "local", "Agent selected a different run mode")
    expect(execution["status"] == "completed", "local Agent task did not complete")
    expect(execution["task_id"], "local Agent task did not receive a task id")
    expect(execution["tool_calls"], "local Agent task did not call a registered tool")
    expect(all(call["name"] in tool_names for call in execution["tool_calls"]),
           "Agent trace contains an unregistered tool call")
    expect(all(call["execution_status"] == "success" and call["business_status"] == "success"
               for call in execution["tool_calls"]),
           "registered tool execution or policy operation failed")
    expect(any(row["policy_id"] == item["policy_id"] for row in execution["evidence"]),
           "Agent evidence does not reference the stored policy")
    expect(execution["trace"]["task_id"] == execution["task_id"],
           "Agent trace task id does not match the completed task")
    expect(execution["trace"]["events"], "Agent returned no real trace events")
    expect(execution["external_model_calls"] == 0 and execution["usage"]["total_tokens"] is None,
           "local Agent falsely reported external model calls or measured token usage")
    passed.append("Agent invoked registered local policy tools and returned a real task trace")

    limited = client.request("POST", "/api/workbench/runs", {**request, "max_rounds": 1})
    expect(limited[0] == 200 and limited[1]["data"]["status"] == "max_rounds",
           "runtime did not surface the task's round limit")
    expect(limited[1]["data"]["failure"]["code"] == "max_rounds",
           "budget stop does not include the runtime failure reason")
    unauthorized = client.request("POST", "/api/workbench/runs", {**request, "mode": "live"})
    expect(unauthorized[0] == 403, "live model invocation was accepted without confirmation")
    passed.append("Agent exposes budget stops and requires confirmation for external model calls")

    before_rejection_hash = database_hash(db_path)
    for method, path in (
        ("POST", "/api/workbench/policies"),
        ("PUT", "/api/workbench/policies"),
        ("PATCH", "/api/workbench/policies/" + item["policy_id"]),
        ("DELETE", "/api/workbench/policies/" + item["policy_id"]),
        ("DELETE", "/api/workbench/fetches"),
    ):
        status, _, _ = client.request(method, path, {} if method in ("POST", "PUT", "PATCH") else None)
        expect(status in (404, 405), f"unexpectedly exposed {method} {path}")
    invalid = client.request("POST", "/api/workbench/ingestion/replay", {
        "fetch_ids": ["outside/invalid.json"],
    })
    expect(invalid[0] == 422, "replay accepted an arbitrary filesystem path")
    extra = client.request("POST", "/api/workbench/ingestion/replay", {
        "fetch_ids": [policy_archives[0]["fetch_id"]],
        "url": "https://example.invalid/",
    })
    expect(extra[0] == 422, "replay accepted an unrecognized URL field")
    duplicate = client.request("POST", "/api/workbench/ingestion/replay", {
        "fetch_ids": [policy_archives[0]["fetch_id"]] * 2,
    })
    expect(duplicate[0] == 422, "replay accepted a duplicate fetch id")
    missing = client.request("GET", "/api/workbench/archive/" + "0" * 32)
    expect(missing[0] == 404, "archive endpoint did not reject an unknown id")
    expect(database_hash(db_path) == before_rejection_hash,
           "a rejected request changed the real policy database")
    passed.append("Write requests, arbitrary URLs, paths, and malformed replay inputs were rejected")

    expect(database_hash(db_path) == before_hash, "integration checks changed the real policy database")
    passed.append("Production policy database SHA-256 is unchanged")

    return {
        "status": "passed",
        "base_url": base_url,
        "project_root": str(project),
        "sqlite_counts": sql_counts,
        "source_count": len(sources["items"]),
        "candidate_count": len(candidates),
        "archive_count": len(archive["items"]),
        "run_count": len(run_list),
        "agent_task_id": execution["task_id"],
        "agent_tool_calls": [call["name"] for call in execution["tool_calls"]],
        "checks": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--project-root",
        type=Path,
        required=True,
        help="PowerCopy V2 project that owns storage/policies_versioned.db",
    )
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    try:
        result = run_checks(args.base_url, args.project_root)
    except (CheckFailure, OSError, URLError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(f"[FAIL] {result['error']}", file=sys.stderr)
    else:
        for check in result["checks"]:
            print(f"[PASS] {check}")
        print(json.dumps({key: value for key, value in result.items() if key != "checks"},
                         ensure_ascii=False, indent=2))

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
