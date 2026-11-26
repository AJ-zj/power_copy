"""Run the workbench runtime checks and optionally the original V2 tests offline."""

import argparse
from contextlib import closing
import os
from pathlib import Path
from shutil import rmtree
import sqlite3
import sys
from unittest.mock import patch
from uuid import uuid4


WORKSPACE = Path(__file__).resolve().parents[1]
PREFIX = "/api/workbench"


def verify_runtime(project: Path) -> None:
    from fastapi.testclient import TestClient
    from workbench.main import app
    from powercopy.core.retry import RetryPolicy

    database = project / "storage" / "policies_versioned.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        expected = dict(conn.execute("SELECT policy_id, MAX(version) FROM policy_versions GROUP BY policy_id"))

    with TestClient(app) as client:
        def run(**data):
            return client.post(f"{PREFIX}/runs", json={"task": "查询本地电价记录", "keyword": "电价", **data})

        def result(response):
            assert response.status_code == 200, response.text
            return response.json()["data"]

        def error(response, status, code):
            assert response.status_code == status, response.text
            assert response.json()["error"]["code"] == code, response.text

        runtime = result(client.get(f"{PREFIX}/runtime"))
        assert runtime["local_available"] and not runtime["live_enabled"]
        assert not runtime["streaming"] and not runtime["history"]
        tools = result(client.get(f"{PREFIX}/tools"))["items"]
        assert {tool["name"] for tool in tools} == {"search_policy", "get_policy_detail"}
        assert all(tool["parameters"]["additionalProperties"] is False for tool in tools)
        print("PASS  Runtime 与真实注册工具", flush=True)

        data = result(run())
        assert data["status"] == "completed" and data["trace"]["status"] == "success"
        assert data["rounds"] == data["model_calls"] == 3
        assert data["external_model_calls"] == 0
        assert data["usage"]["total_tokens"] is None and data["budget"]["usage_incomplete"]
        assert data["task_id"] == data["trace"]["task_id"]
        events = [event["event_type"] for event in data["trace"]["events"]]
        assert events.count("model_attempt") == 3 and events.count("tool_execution") == 2
        assert [tool["name"] for tool in data["tool_calls"]] == ["search_policy", "get_policy_detail"]
        assert all(tool["business_status"] == "success" for tool in data["tool_calls"])
        assert data["evidence"] and any(item["kind"] == "detail" for item in data["evidence"])
        assert all(expected[item["policy_id"]] == item["version"] for item in data["evidence"])
        print("PASS  本地 Mock LLM、真实 SQLite 政策工具与 Trace", flush=True)

        data = result(run(keyword=f"未命中-{uuid4().hex}"))
        assert data["status"] == "completed" and data["evidence"] == []
        assert data["tool_calls"][0]["result"]["returned"] == 0
        print("PASS  无匹配政策", flush=True)

        data = result(run(max_rounds=1))
        assert data["status"] == "max_rounds" and data["trace"]["status"] == "stopped"
        assert data["model_calls"] == 1 and data["tool_requests"] == 0
        assert data["failure"]["code"] == "max_rounds"
        print("PASS  执行轮次上限", flush=True)

        with patch("workbench.runtime_api.database_path", return_value=database.with_name(f"missing-{uuid4().hex}.db")):
            data = result(run())
        tool_call = data["tool_calls"][0]
        assert data["status"] == "completed" and data["evidence"] == []
        assert tool_call["execution_status"] == "success" and tool_call["business_status"] == "failed"
        assert tool_call["error_type"] == "POLICY_DB_NOT_FOUND"
        print("PASS  区分工具执行、业务操作与任务状态", flush=True)

        private_message = "synthetic-private-provider-error"
        with (
            patch("workbench.runtime_api.LocalPolicyLLM.create_completion", side_effect=RuntimeError(private_message)),
            patch("workbench.runtime_api.RetryPolicy", return_value=RetryPolicy(max_attempts=2)),
            patch.object(RetryPolicy, "should_retry", return_value=True),
            patch("powercopy.core.llm.time.sleep"),
        ):
            data = result(run())
        assert data["status"] == "failed" and data["trace"]["status"] == "failed"
        assert data["failure"]["code"] == "AGENT_RUN_FAILED" and private_message not in str(data)
        assert any(event["event_type"] == "task_error" for event in data["trace"]["events"])
        attempts = [event for event in data["trace"]["events"] if event["event_type"] == "model_attempt"]
        assert [(event["attempt"], event["is_retry"]) for event in attempts] == [(1, False), (2, True)]
        print("PASS  失败 Trace、重试与异常脱敏", flush=True)

        error(run(mode="live", confirm_live=True), 403, "LIVE_MODEL_DISABLED")
        print("PASS  真实模型默认关闭", flush=True)

        response = run(max_rounds=999, api_key=private_message)
        error(response, 422, "INVALID_INPUT")
        assert private_message not in response.text
        print("PASS  请求校验和敏感输入脱敏", flush=True)


def verify_core(project: Path) -> int:
    import pytest

    scratch = WORKSPACE / "workspace"
    scratch.mkdir(exist_ok=True)
    mkdir = Path.mkdir

    # A unique directory is created with inherited permissions before pytest runs.
    root = scratch / f"workbench-pytest-{uuid4().hex}"
    root.mkdir()
    original_directory = Path.cwd()

    try:
        def test_mkdir(path, mode=0o777, parents=False, exist_ok=False):
            # Windows 3.12.14's mode 0700 ACL excludes the desktop restricted token.
            # Allow test directories to inherit their workspace's normal ACLs.
            if os.name == "nt" and mode == 0o700 and path.is_relative_to(root):
                mode = 0o777
            return mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        with patch.object(Path, "mkdir", test_mkdir):
            os.chdir(project)
            return pytest.main(["-q", "-p", "no:cacheprovider", "--basetemp", str(root / "fixtures")])
    finally:
        os.chdir(original_directory)
        rmtree(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=os.environ.get("POWERCOPY_PROJECT_ROOT"))
    parser.add_argument("--core", action="store_true", help="also run the original V2 test suite")
    args = parser.parse_args()
    project = (args.project_root or WORKSPACE.parent.parent / "PowerCopy-V2").resolve()
    os.environ["POWERCOPY_PROJECT_ROOT"] = str(project)
    os.environ["POWERCOPY_WORKBENCH_ALLOW_LIVE"] = "0"
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(project), str(WORKSPACE), str(WORKSPACE / ".workbench-deps")]
    core_status = verify_core(project) if args.core else 0
    verify_runtime(project)
    return core_status


if __name__ == "__main__":
    raise SystemExit(main())
