from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from powercopy.data.models import PolicyDocument
from powercopy.data.repository import PolicyRepository


@pytest.fixture
def payload():
    return {
        "source_key": "demo",
        "source_url": "https://example.invalid/policy/1",
        "title": "模拟售电政策",
        "body_text": "仅用于测试，不是真实政策。\n第二段。",
        "region": "安徽",
        "retrieved_at": datetime(
            2026, 11, 25,
            tzinfo=timezone.utc,
        ),
    }


@pytest.fixture
def repo(tmp_path):
    return PolicyRepository(tmp_path / "policies.db")


# ---------------------------------------------------------------- 1
class TestDataContract:
    """验收 1：PolicyDocument 数据契约校验通过。"""

    def test_valid_document(self, payload):
        doc = PolicyDocument(**payload)
        assert doc.source_key == "demo"
        assert doc.title == "模拟售电政策"
        assert doc.region == "安徽"
        assert doc.document_id is not None

    def test_document_id_generated_per_instance(self, payload):
        first = PolicyDocument(**payload)
        second = PolicyDocument(**payload)
        assert first.document_id != second.document_id

    def test_optional_fields_default_none(self, payload):
        doc = PolicyDocument(**payload)
        assert doc.issuer is None
        assert doc.published_on is None

    def test_optional_fields_can_be_set(self, payload):
        doc = PolicyDocument(
            **payload,
            issuer="安徽省发展改革委",
            published_on=date(2025, 5, 30),
        )
        assert doc.issuer == "安徽省发展改革委"
        assert doc.published_on == date(2025, 5, 30)

    def test_extra_field_forbidden(self, payload):
        with pytest.raises(ValidationError):
            PolicyDocument(**payload, unknown_field="x")

    def test_frozen_cannot_mutate(self, payload):
        doc = PolicyDocument(**payload)
        with pytest.raises(ValidationError):
            doc.title = "改掉"

    def test_json_round_trip(self, payload):
        doc = PolicyDocument(**payload)
        restored = PolicyDocument.model_validate_json(doc.model_dump_json())
        assert restored == doc


# ---------------------------------------------------------------- 2
class TestInvalidData:
    """验收 2：非法或空白数据被正确拒绝。"""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "  "),
            ("title", ""),
            ("title", "\n\t "),
            ("body_text", "\n  "),
            ("body_text", ""),
            ("source_key", ""),
            ("source_key", "   "),
        ],
    )
    def test_blank_fields_rejected(self, payload, field, value):
        payload[field] = value
        with pytest.raises(ValidationError):
            PolicyDocument(**payload)

    def test_missing_required_field(self, payload):
        payload.pop("title")
        with pytest.raises(ValidationError):
            PolicyDocument(**payload)

    def test_title_is_stripped(self, payload):
        payload["title"] = "  模拟政策  "
        doc = PolicyDocument(**payload)
        assert doc.title == "模拟政策"


# ---------------------------------------------------------------- 3
class TestUrlAndTimezone:
    """验收 3：来源 URL 与采集时区校验通过。"""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/policy",
            "not-a-url",
            "https://",
            "",
        ],
    )
    def test_invalid_url_rejected(self, payload, url):
        payload["source_url"] = url
        with pytest.raises(ValidationError):
            PolicyDocument(**payload)

    def test_http_and_https_accepted(self, payload):
        for url in ("http://example.invalid/a", "https://example.invalid/a"):
            payload["source_url"] = url
            assert PolicyDocument(**payload).source_url

    def test_naive_datetime_rejected(self, payload):
        payload["retrieved_at"] = datetime(2026, 11, 25)
        with pytest.raises(ValidationError):
            PolicyDocument(**payload)

    def test_aware_datetime_required(self, payload):
        from datetime import timedelta

        payload["retrieved_at"] = datetime(
            2026, 11, 25, tzinfo=timezone(timedelta(hours=8))
        )
        doc = PolicyDocument(**payload)
        assert doc.retrieved_at.tzinfo is not None


# ---------------------------------------------------------------- 4
class TestWriteAndRead:
    """验收 4：SQLite 写入、读取与对象还原正确。"""

    def test_add_returns_document_id(self, repo, payload):
        doc = PolicyDocument(**payload)
        returned = repo.add(doc)
        assert returned == doc.document_id

    def test_get_restores_object(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)

        loaded = repo.get(doc.document_id)
        assert loaded is not None
        assert loaded == doc, "读回来的对象应与写入时完全一致"

    def test_get_by_string_id(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)

        loaded = repo.get(str(doc.document_id))
        assert loaded == doc

    def test_all_fields_persisted(self, repo, payload):
        doc = PolicyDocument(
            **payload,
            issuer="安徽省发展改革委",
            published_on=date(2025, 5, 30),
        )
        repo.add(doc)
        loaded = repo.get(doc.document_id)

        assert loaded.source_key == doc.source_key
        assert str(loaded.source_url) == str(doc.source_url)
        assert loaded.title == doc.title
        assert loaded.body_text == doc.body_text
        assert loaded.region == doc.region
        assert loaded.issuer == doc.issuer
        assert loaded.published_on == doc.published_on
        assert loaded.retrieved_at == doc.retrieved_at

    def test_optional_none_persisted(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)
        loaded = repo.get(doc.document_id)
        assert loaded.region == "安徽"
        assert loaded.issuer is None
        assert loaded.published_on is None

    def test_multiline_body_preserved(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)
        loaded = repo.get(doc.document_id)
        assert "\n" in loaded.body_text
        assert loaded.body_text == payload["body_text"]

    def test_chinese_text_preserved(self, repo, payload):
        payload["title"] = "安徽省工商业峰谷分时电价政策"
        payload["body_text"] = "低谷电价下浮 61.8%，夏冬季高峰上浮 84.3%。"
        doc = PolicyDocument(**payload)
        repo.add(doc)

        loaded = repo.get(doc.document_id)
        assert loaded.title == "安徽省工商业峰谷分时电价政策"
        assert "61.8%" in loaded.body_text


# ---------------------------------------------------------------- 5
class TestPersistence:
    """验收 5：重新创建 Repository 后数据依然存在。"""

    def test_data_survives_new_instance(self, tmp_path, payload):
        db_path = tmp_path / "policies.db"

        doc = PolicyDocument(**payload)
        PolicyRepository(db_path).add(doc)

        # 完全重建 Repository（模拟进程重启）
        reopened = PolicyRepository(db_path)
        loaded = reopened.get(doc.document_id)
        assert loaded == doc

    def test_multiple_documents_survive(self, tmp_path, payload):
        db_path = tmp_path / "policies.db"
        repo = PolicyRepository(db_path)

        docs = []
        for i in range(3):
            doc = PolicyDocument(**{**payload, "title": f"政策 {i}"})
            repo.add(doc)
            docs.append(doc)

        reopened = PolicyRepository(db_path)
        for doc in docs:
            assert reopened.get(doc.document_id) == doc


# ---------------------------------------------------------------- 6
class TestDuplicateKey:
    """验收 6：重复主键被拒绝，原数据不受影响。"""

    def test_duplicate_id_rejected(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)

        with pytest.raises(Exception) as exc:
            repo.add(doc)
        assert "UNIQUE" in str(exc.value).upper() or "PRIMARY" in str(exc.value).upper()

    def test_original_data_untouched(self, repo, payload):
        doc = PolicyDocument(**payload)
        repo.add(doc)

        # 尝试用同 ID 但不同内容写入
        duplicate = doc.model_copy(update={"title": "试图覆盖"})
        with pytest.raises(Exception):
            repo.add(duplicate)

        loaded = repo.get(doc.document_id)
        assert loaded.title == doc.title, "原数据不能被覆盖"

    def test_same_content_different_id_allowed(self, repo, payload):
        first = PolicyDocument(**payload)
        second = PolicyDocument(**payload)
        assert first.document_id != second.document_id

        repo.add(first)
        repo.add(second)

        assert repo.get(first.document_id) is not None
        assert repo.get(second.document_id) is not None


# ---------------------------------------------------------------- 7
class TestUnknownAndSnapshots:
    """验收 7：未知文档与独立快照处理正确。"""

    def test_unknown_document_returns_none(self, repo):
        assert repo.get(uuid4()) is None

    def test_unknown_string_id_returns_none(self, repo):
        assert repo.get("00000000-0000-0000-0000-000000000000") is None

    def test_two_snapshots_of_same_source_independent(self, repo, payload):
        """同一来源的两次采集作为两个快照，互不影响。"""
        first = PolicyDocument(**payload)
        second = PolicyDocument(
            **{**payload, "body_text": "更新后的正文内容。"}
        )
        repo.add(first)
        repo.add(second)

        loaded_first = repo.get(first.document_id)
        loaded_second = repo.get(second.document_id)

        assert loaded_first.body_text == payload["body_text"]
        assert loaded_second.body_text == "更新后的正文内容。"
        assert loaded_first.document_id != loaded_second.document_id


# ---------------------------------------------------------------- 8
class TestParameterizedSql:
    """验收 8：参数化 SQL 测试通过。"""

    def test_sql_injection_in_title_is_safe(self, repo, payload):
        payload["title"] = "'; DROP TABLE policy_documents; --"
        doc = PolicyDocument(**payload)
        repo.add(doc)

        loaded = repo.get(doc.document_id)
        assert loaded.title == "'; DROP TABLE policy_documents; --"

        # 表仍然存在
        assert repo.get(uuid4()) is None

    def test_find_by_title_with_quote(self, repo, payload):
        payload["title"] = "政策' OR '1'='1"
        doc = PolicyDocument(**payload)
        repo.add(doc)

        results = repo.find_by_title("政策' OR '1'='1")
        assert len(results) == 1
        assert results[0].document_id == doc.document_id

    def test_find_by_title_no_match(self, repo, payload):
        repo.add(PolicyDocument(**payload))
        assert repo.find_by_title("不存在的标题") == []

    def test_find_by_title_exact_match_only(self, repo, payload):
        repo.add(PolicyDocument(**payload))
        # 部分匹配不应命中
        assert repo.find_by_title("模拟售电") == []


# ---------------------------------------------------------------- 附加
class TestRepositoryInit:
    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "policies.db"
        PolicyRepository(nested)
        assert nested.parent.is_dir()

    def test_idempotent_schema_creation(self, tmp_path):
        db_path = tmp_path / "policies.db"
        PolicyRepository(db_path)
        PolicyRepository(db_path)  # 重复初始化不应报错

    def test_table_created(self, tmp_path, payload):
        repo = PolicyRepository(tmp_path / "policies.db")
        assert repo.get(uuid4()) is None
