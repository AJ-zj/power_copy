"""4.5 验收：真实原始数据离线 Pipeline。

依赖 storage/raw/ 真实采集文件与 config/ 配置；缺失时整组跳过。
"""

import base64
import json
from pathlib import Path

import pytest

from powercopy.data.pipeline import PipelineError, discover, save_fetch
from powercopy.data.parser import RawPage, load_raw
from powercopy.data.versioning import VersionedPolicyStore

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "storage" / "raw"
PROFILES = ROOT / "config" / "parser_profiles.json"
SELECTORS = ROOT / "config" / "index_selectors.json"

NDRC = RAW / "722b8057489c4ac2a54461d62dad117a.json"
NEA = RAW / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"
NEWS = RAW / "9e40d80d358447a18240db6625a3c6e7.json"
INDEX = RAW / "fd45e1c38564451b8e1d382360ade61c.json"

pytestmark = pytest.mark.skipif(
    not all(p.is_file() for p in (NDRC, NEA, NEWS, INDEX))
    or not PROFILES.is_file()
    or not SELECTORS.is_file(),
    reason="缺少真实采集文件或配置，跳过真实数据 Pipeline 测试",
)


@pytest.fixture(scope="module")
def profiles():
    return json.loads(PROFILES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def selectors():
    return json.loads(SELECTORS.read_text(encoding="utf-8"))


@pytest.fixture
def pipeline(tmp_path, profiles, selectors):
    from powercopy.data.pipeline import IngestionPipeline

    store = VersionedPolicyStore(tmp_path / "v.db")
    return IngestionPipeline(
        store,
        profiles,
        raw_dir=tmp_path / "raw",
        run_dir=tmp_path / "runs",
    ), store


# ---------------------------------------------------------------- 1 完整串联
class TestOfflinePipeline:
    def test_two_real_policies_created(self, pipeline):
        pipe, store = pipeline
        report = pipe.replay([NDRC, NEA])

        assert report["mode"] == "offline"
        assert report["status"] == "success"
        assert report["requests"] == 0, "离线 replay 不得发请求"

        actions = [item["result"]["action"] for item in report["items"]]
        assert actions == ["created", "created"]
        assert store.counts() == dict(
            policies=2, versions=2, fetches=2, documents=2
        )

    def test_report_has_raw_path_and_fetch_id(self, pipeline):
        pipe, _ = pipeline
        report = pipe.replay([NDRC])
        item = report["items"][0]
        assert item["stage"] == "process"
        assert item["status"] == "success"
        assert item["fetch_id"] == load_raw(NDRC).fetch_id
        assert item["raw_path"] == str(NDRC)

    def test_report_persisted_to_disk(self, pipeline):
        pipe, _ = pipeline
        report = pipe.replay([NDRC])
        path = pipe.run_dir / f"{report['run_id']}.json"
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8")) == report


# ---------------------------------------------------------------- 2 重复重放
class TestReplayIdempotency:
    def test_replay_same_files(self, pipeline):
        pipe, store = pipeline
        first = pipe.replay([NDRC, NEA])
        second = pipe.replay([NDRC, NEA])

        assert [i["result"]["action"] for i in first["items"]] == [
            "created",
            "created",
        ]
        assert [i["result"]["action"] for i in second["items"]] == [
            "replayed",
            "replayed",
        ]
        assert store.counts()["fetches"] == 2

    def test_replay_preserves_document_ids(self, pipeline):
        pipe, _ = pipeline
        first = pipe.replay([NDRC])
        second = pipe.replay([NDRC])
        assert (
            first["items"][0]["result"]["document_id"]
            == second["items"][0]["result"]["document_id"]
        )


# ---------------------------------------------------------------- 3 四种动作
class TestFourActions:
    def test_unchanged_action(self, pipeline, tmp_path):
        """同内容不同 fetch_id → unchanged。"""
        pipe, store = pipeline
        first = pipe.replay([NDRC])
        assert first["items"][0]["result"]["action"] == "created"

        raw = load_raw(NDRC)
        body = raw.body_bytes()
        variant = RawPage(
            fetch_id="variant-fetch-id",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at.replace(microsecond=raw.retrieved_at.microsecond)
            if False
            else raw.retrieved_at,
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        path = tmp_path / "variant.json"
        data = variant.model_dump(mode="json")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        # 时间必须晚于首次采集，才不会被判定为迟到
        from datetime import timedelta

        later = variant.model_copy(update={"retrieved_at": raw.retrieved_at + timedelta(hours=1)})
        data = later.model_dump(mode="json")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        report = pipe.replay([path])
        assert report["items"][0]["result"]["action"] == "unchanged"
        assert store.counts()["versions"] == 1

    def test_updated_action(self, pipeline, tmp_path):
        """正文变化 → updated（新版本）。"""
        pipe, store = pipeline
        first = pipe.replay([NDRC])
        assert first["items"][0]["result"]["action"] == "created"

        raw = load_raw(NDRC)
        html = raw.body_bytes().decode("utf-8").replace(
            "本规则自2024年7月1日起施行", "本规则自2025年1月1日起施行"
        )
        body = html.encode("utf-8")
        from datetime import timedelta

        changed = RawPage(
            fetch_id="updated-fetch-id",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at + timedelta(days=10),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        path = tmp_path / "changed.json"
        path.write_text(
            json.dumps(changed.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )

        report = pipe.replay([path])
        result = report["items"][0]["result"]
        assert result["action"] == "updated"
        assert result["version"] == 2
        assert store.counts()["versions"] == 2

    def test_replayed_action(self, pipeline):
        pipe, _ = pipeline
        pipe.replay([NDRC])
        report = pipe.replay([NDRC])
        assert report["items"][0]["result"]["action"] == "replayed"


# ---------------------------------------------------------------- 4 保存优先
class TestSaveBeforeParse:
    def test_raw_saved_before_parse_failure(self, pipeline, tmp_path, profiles):
        """解析配置错误时，raw 文件仍应已保存，可用于修复后恢复。"""
        pipe, store = pipeline
        broken_profiles = dict(profiles)
        broken_profiles["ndrc/market_rules"] = {
            **profiles["ndrc/market_rules"],
            "body_selector": "#selector-that-does-not-exist",
        }

        from powercopy.data.pipeline import IngestionPipeline

        broken_pipe = IngestionPipeline(
            store,
            broken_profiles,
            raw_dir=tmp_path / "raw",
            run_dir=tmp_path / "runs",
        )
        report = broken_pipe.replay([NDRC])
        item = report["items"][0]

        assert item["status"] == "failed"
        assert item["error_code"].startswith("SELECTOR_MATCH_COUNT")
        assert item["raw_path"] == str(NDRC)

        # 修复配置后用同一 raw 离线恢复
        fixed = IngestionPipeline(
            store,
            profiles,
            raw_dir=tmp_path / "raw",
            run_dir=tmp_path / "runs",
        )
        recovered = fixed.replay([NDRC])
        assert recovered["items"][0]["result"]["action"] == "created"
        assert store.counts()["documents"] == 1

    def test_save_fetch_writes_valid_raw(self, tmp_path):
        """用项目真实的 FetchResult dataclass 验证 save_fetch 兼容性。"""
        from datetime import datetime, timezone

        from powercopy.data.fetcher import FetchResult

        raw = load_raw(NDRC)
        result = FetchResult(
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=str(raw.source_url),
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            body=raw.body_bytes(),
            body_size=raw.body_size,
            duration_ms=1.0,
            retrieved_at=datetime.now(timezone.utc),
        )

        path = save_fetch(result, tmp_path / "raw")
        assert path.is_file()
        reloaded = load_raw(path)
        assert reloaded.body_bytes() == raw.body_bytes()
        assert reloaded.source_url == raw.source_url
        assert reloaded.content_kind == raw.content_kind


# ---------------------------------------------------------------- 8 真实列表页
class TestRealIndexDiscovery:
    def test_selector_matches_real_links(self, selectors):
        selector = selectors["nea_east/policy_index"]
        assert "REPLACE_WITH" not in selector, "选择器仍是占位符"

    def test_discover_candidates(self, selectors):
        raw = load_raw(INDEX)
        candidates = discover(raw, selectors["nea_east/policy_index"], limit=20)

        assert len(candidates) == 15
        assert all(c["status"] == "pending_review" for c in candidates)
        assert all(c["url"].startswith("https://") for c in candidates)
        assert all("hdj.nea.gov.cn" in c["url"] for c in candidates)
        assert len({c["url"] for c in candidates}) == len(candidates)

    def test_candidate_titles_are_real_headings(self, selectors):
        raw = load_raw(INDEX)
        candidates = discover(raw, selectors["nea_east/policy_index"], limit=5)
        titles = [c["title"] for c in candidates]
        assert any("安徽" in t for t in titles)
        assert any("长三角" in t or "华东" in t for t in titles)

    def test_candidates_not_persisted(self, pipeline, selectors):
        pipe, store = pipeline
        report = pipe.replay([INDEX], selectors)
        assert report["items"][0]["result"]["action"] == "discovered"
        assert store.counts()["documents"] == 0, "候选不得自动入库"
        assert store.counts()["policies"] == 0

    def test_limit_respected(self, selectors):
        raw = load_raw(INDEX)
        for limit in (1, 3, 5):
            found = discover(raw, selectors["nea_east/policy_index"], limit=limit)
            assert len(found) == limit

    def test_zero_match_raises(self):
        raw = load_raw(INDEX)
        with pytest.raises(PipelineError, match="INDEX_SELECTOR_NO_MATCH"):
            discover(raw, "#no-such-selector")

    def test_non_index_kind_rejected(self, selectors):
        raw = load_raw(NDRC)
        with pytest.raises(PipelineError, match="NOT_AN_INDEX"):
            discover(raw, selectors["nea_east/policy_index"])


# ---------------------------------------------------------------- 9 新闻隔离
class TestNewsIsolation:
    def test_market_news_archived_only(self, pipeline):
        pipe, store = pipeline
        report = pipe.replay([NEWS])
        item = report["items"][0]

        assert item["status"] == "success"
        assert item["result"]["action"] == "archived_only"
        assert store.counts()["documents"] == 0, "新闻不得进入政策库"
        assert store.counts()["policies"] == 0


# ---------------------------------------------------------------- 10 失败隔离
class TestFailureIsolation:
    def test_one_failure_does_not_block_others(self, pipeline, tmp_path, profiles):
        """中间一项失败，前后两项仍应成功。"""
        pipe, store = pipeline

        from powercopy.data.pipeline import IngestionPipeline
        from datetime import timedelta

        raw = load_raw(NDRC)
        bad = RawPage(
            fetch_id="bad-parse-fetch",
            source_key="unknown_source_key_not_in_profiles",
            target_key="unknown_target",
            source_url=raw.source_url,
            content_kind="policy",
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            retrieved_at=raw.retrieved_at,
            body_size=raw.body_size,
            body_base64=raw.body_base64,
        )
        bad_path = tmp_path / "bad.json"
        bad_path.write_text(
            json.dumps(bad.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )

        report = pipe.replay([NDRC, bad_path, NEA])
        statuses = [i["status"] for i in report["items"]]
        assert statuses == ["success", "failed", "success"]
        assert report["status"] == "partial"
        assert store.counts()["documents"] == 2
