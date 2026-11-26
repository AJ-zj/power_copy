"""4.4 验收：真实政策离线入库与重放。

依赖 storage/raw/ 里的真实采集文件；缺失时整组跳过。
"""

import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from powercopy.data.parser import RawPage, ParseProfile, load_raw
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore, VersionError

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "storage" / "raw"
PROFILES = ROOT / "config" / "parser_profiles.json"

NDRC = RAW / "722b8057489c4ac2a54461d62dad117a.json"
NEA = RAW / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"
NEWS = RAW / "9e40d80d358447a18240db6625a3c6e7.json"
INDEX = RAW / "fd45e1c38564451b8e1d382360ade61c.json"

pytestmark = pytest.mark.skipif(
    not all(p.is_file() for p in (NDRC, NEA, NEWS, INDEX)) or not PROFILES.is_file(),
    reason="缺少真实采集文件或解析配置，跳过真实数据版本测试",
)


@pytest.fixture(scope="module")
def profiles():
    return json.loads(PROFILES.read_text(encoding="utf-8"))


def profile_for(key, profiles):
    return ParseProfile.model_validate(profiles[key])


@pytest.fixture
def store(tmp_path):
    return VersionedPolicyStore(tmp_path / "versioned.db")


# ---------------------------------------------------------------- 1 身份稳定
class TestStableIdentity:
    def test_same_url_same_policy_id(self, store, profiles):
        raw = load_raw(NDRC)
        first = store.ingest(raw, profile_for("ndrc/market_rules", profiles))
        second = store.ingest(raw, profile_for("ndrc/market_rules", profiles))
        assert first.policy_id == second.policy_id

    def test_two_sources_different_policies(self, store, profiles):
        a = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        b = store.ingest(load_raw(NEA), profile_for("nea/registration_rules", profiles))
        assert a.policy_id != b.policy_id
        assert store.counts()["policies"] == 2


# ---------------------------------------------------------------- 2 首次 V1
class TestFirstIngest:
    def test_creates_version_one(self, store, profiles):
        result = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        assert result.action == "created"
        assert result.version == 1
        assert store.counts() == dict(
            policies=1, versions=1, fetches=1, documents=1
        )

    def test_document_readable(self, store, profiles):
        result = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        doc = PolicyRepository(store.db_path).get(result.document_id)
        assert doc is not None
        assert "电力市场运行基本规则" in doc.title
        assert len(doc.body_text) > 3000

    def test_raw_sha_recorded(self, store, profiles):
        result = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        obs = store.observations(result.policy_id)[0]
        assert len(obs["raw_sha256"]) == 64
        assert len(result.content_sha256) == 64


# ---------------------------------------------------------------- 3 相同内容去重
class TestSameContentDedup:
    def test_second_fetch_same_content_unchanged(self, store, profiles):
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        first = store.ingest(raw, profile)

        # 用同一 HTML 但新的 fetch_id 和更晚采集时间，模拟再次采集
        body = raw.body_bytes()
        import base64

        later = RawPage(
            fetch_id="second-fetch-id",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at + timedelta(hours=1),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        second = store.ingest(later, profile)

        assert second.action == "unchanged"
        assert second.document_id == first.document_id
        assert store.counts() == dict(
            policies=1, versions=1, fetches=2, documents=1
        )


# ---------------------------------------------------------------- 4 幂等与冲突
class TestFetchIdempotency:
    def test_replay_same_fetch_id(self, store, profiles):
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        first = store.ingest(raw, profile)
        second = store.ingest(raw, profile)
        assert second.action == "replayed"
        assert second.document_id == first.document_id
        assert store.counts()["fetches"] == 1

    def test_same_fetch_id_different_file_conflict(self, store, profiles):
        profile = profile_for("ndrc/market_rules", profiles)
        raw = load_raw(NDRC)
        store.ingest(raw, profile)

        # 同一个 fetch_id，但原始字节被改动（模拟文件被替换）
        html = raw.body_bytes().decode("utf-8").replace(
            "本规则自2024年7月1日起施行",
            "本规则自2025年1月1日起施行",
        )
        body = html.encode("utf-8")
        import base64

        forged = RawPage(
            fetch_id=raw.fetch_id,
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at,
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        with pytest.raises(VersionError, match="FETCH_ID_CONFLICT"):
            store.ingest(forged, profile)

        assert store.counts()["fetches"] == 1


# ---------------------------------------------------------------- 5 内容变化新版
class TestContentChange:
    def test_modified_body_creates_version_two(self, tmp_path, profiles):
        store = VersionedPolicyStore(tmp_path / "v.db")
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        first = store.ingest(raw, profile)

        # 基于真实 HTML 改动正文，模拟政策修订
        html = raw.body_bytes().decode("utf-8")
        modified = html.replace(
            "本规则自2024年7月1日起施行",
            "本规则自2025年1月1日起施行",
        )
        assert modified != html, "测试必须真正改动到正文"

        import base64

        body = modified.encode("utf-8")
        changed = RawPage(
            fetch_id="revised-fetch",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at + timedelta(days=30),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        second = store.ingest(changed, profile)

        assert second.action == "updated"
        assert second.version == 2
        assert second.policy_id == first.policy_id
        assert second.content_sha256 != first.content_sha256
        assert store.counts() == dict(
            policies=1, versions=2, fetches=2, documents=2
        )


# ---------------------------------------------------------------- 6 A→B→A
class TestRecurrence:
    def test_a_b_a_keeps_full_history(self, tmp_path, profiles):
        store = VersionedPolicyStore(tmp_path / "v.db")
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        original = raw.body_bytes()

        def variant(fetch_id, old, new, days):
            html = original.decode("utf-8").replace(old, new)
            body = html.encode("utf-8")
            import base64

            return RawPage(
                fetch_id=fetch_id,
                source_key=raw.source_key,
                target_key=raw.target_key,
                source_url=raw.source_url,
                content_kind=raw.content_kind,
                status_code=raw.status_code,
                content_type=raw.content_type,
                charset=raw.charset,
                retrieved_at=raw.retrieved_at + timedelta(days=days),
                body_size=len(body),
                body_base64=base64.b64encode(body).decode(),
            )

        a1 = store.ingest(raw, profile)
        b = store.ingest(
            variant("v-b", "本规则自2024年7月1日起施行", "本规则自2025年1月1日起施行", 10),
            profile,
        )
        a2 = store.ingest(
            variant("v-a2", "本规则自2025年1月1日起施行", "本规则自2024年7月1日起施行", 20),
            profile,
        )

        assert [a1.version, b.version, a2.version] == [1, 2, 3]
        assert a1.content_sha256 == a2.content_sha256
        assert a1.content_sha256 != b.content_sha256

        history = store.history(a1.policy_id)
        assert len(history) == 3
        assert [h["version"] for h in history] == [1, 2, 3]

        observations = store.observations(a1.policy_id)
        assert len(observations) == 3

        # 三个版本对应三份独立文档快照
        docs = {h["document_id"] for h in history}
        assert len(docs) == 3


# ---------------------------------------------------------------- 7 哈希与血缘
class TestHashAndLineage:
    def test_raw_hash_differs_content_hash_same(self, tmp_path, profiles):
        store = VersionedPolicyStore(tmp_path / "v.db")
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        first = store.ingest(raw, profile)

        # 加一段页脚噪音：原始字节变了，但正文没变
        html = raw.body_bytes().decode("utf-8").replace(
            "</body>", "<footer>新页脚</footer></body>"
        )
        body = html.encode("utf-8")
        import base64

        noised = RawPage(
            fetch_id="noise-fetch",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at + timedelta(hours=2),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        second = store.ingest(noised, profile)

        assert second.action == "unchanged"
        obs = store.observations(first.policy_id)
        assert obs[0]["raw_sha256"] != obs[1]["raw_sha256"], "原始字节哈希应不同"
        assert first.content_sha256 == second.content_sha256, "内容哈希应相同"

    def test_lineage_links_fetch_to_version(self, store, profiles):
        result = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        obs = store.observations(result.policy_id)
        assert obs[0]["fetch_id"] == result.fetch_id
        assert obs[0]["version"] == result.version
        assert obs[0]["policy_id"] == result.policy_id
        assert obs[0]["target_key"] == "market_rules"


# ---------------------------------------------------------------- 8 事务与外键
class TestTransactionAndForeignKeys:
    def test_failed_insert_rolls_back_everything(self, store, profiles):
        import sqlite3

        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_fetch BEFORE INSERT ON policy_fetches "
                "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
            )

        with pytest.raises(sqlite3.IntegrityError):
            store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))

        assert store.counts() == dict(
            policies=0, versions=0, fetches=0, documents=0
        ), "失败后不能留下任何半成品数据"

    def test_foreign_keys_enabled(self, store):
        import sqlite3

        conn = sqlite3.connect(store.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO policy_versions VALUES ('ghost', 1, 'doc', 'hash')"
                )
        finally:
            conn.close()

    def test_untracked_documents_guard(self, tmp_path, profiles):
        """已有未纳管的旧文档时，拒绝初始化版本库。"""
        from powercopy.data.parser import parse_article, to_policy_document

        db = tmp_path / "legacy.db"
        repo = PolicyRepository(db)
        raw = load_raw(NDRC)
        document = to_policy_document(
            raw, parse_article(raw, profile_for("ndrc/market_rules", profiles))
        )
        repo.add(document)

        with pytest.raises(VersionError, match="UNTRACKED_DOCUMENTS"):
            VersionedPolicyStore(db)


# ---------------------------------------------------------------- 9 迟到与非法
class TestLateAndInvalid:
    def test_late_fetch_rejected(self, tmp_path, profiles):
        store = VersionedPolicyStore(tmp_path / "v.db")
        raw = load_raw(NDRC)
        profile = profile_for("ndrc/market_rules", profiles)
        store.ingest(raw, profile)

        import base64

        body = raw.body_bytes()
        late = RawPage(
            fetch_id="late-fetch",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at - timedelta(days=1),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        with pytest.raises(VersionError, match="OUT_OF_ORDER_FETCH"):
            store.ingest(late, profile)
        assert store.counts()["fetches"] == 1

    def test_market_news_rejected(self, store, profiles):
        from powercopy.data.normalizer import ParseError

        with pytest.raises(ParseError, match="NOT_A_POLICY"):
            store.ingest(
                load_raw(NEWS), profile_for("nea_east/anhui_market_news", profiles)
            )
        assert store.counts()["documents"] == 0

    def test_index_rejected(self, store, profiles):
        """列表页 content_kind=index，先于 profile 校验就被拒绝。"""
        from powercopy.data.normalizer import ParseError

        index_raw = load_raw(INDEX)
        assert index_raw.content_kind == "index"

        # index 没有 profile，用任意合法 profile 也能被 content_kind 拦下
        fallback = ParseProfile(title_selector="title", body_selector=".wrapper")
        with pytest.raises(ParseError, match="NOT_AN_ARTICLE"):
            store.ingest(index_raw, fallback)
        assert store.counts()["documents"] == 0

    def test_blank_fetch_id_rejected(self, store, profiles):
        raw = load_raw(NDRC)
        import base64

        body = raw.body_bytes()
        broken = RawPage(
            fetch_id="   ",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at,
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        with pytest.raises(VersionError, match="INVALID_FETCH_ID_OR_TARGET"):
            store.ingest(broken, profile_for("ndrc/market_rules", profiles))


# ---------------------------------------------------------------- 10 真实入库与重放
class TestRealIngestAndReplay:
    def test_two_real_policies_ingested(self, store, profiles):
        ndrc = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        nea = store.ingest(load_raw(NEA), profile_for("nea/registration_rules", profiles))

        assert ndrc.action == "created" and ndrc.version == 1
        assert nea.action == "created" and nea.version == 1
        assert ndrc.policy_id != nea.policy_id

        counts = store.counts()
        assert counts == dict(policies=2, versions=2, fetches=2, documents=2)

    def test_replay_both_policies(self, store, profiles):
        store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        store.ingest(load_raw(NEA), profile_for("nea/registration_rules", profiles))

        again_ndrc = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        again_nea = store.ingest(load_raw(NEA), profile_for("nea/registration_rules", profiles))

        assert again_ndrc.action == "replayed"
        assert again_nea.action == "replayed"
        assert store.counts()["fetches"] == 2, "重放不应新增 fetch 记录"

    def test_documents_readable_after_replay(self, store, profiles):
        result = store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))
        store.ingest(load_raw(NDRC), profile_for("ndrc/market_rules", profiles))

        doc = PolicyRepository(store.db_path).get(result.document_id)
        assert doc is not None
        assert doc.published_on.isoformat() == "2024-04-25"
        assert "第四十五条" in doc.body_text
