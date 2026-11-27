"""本地 Embedding 编码器（BGE-small-zh-v1.5）。

责任边界
--------
只做「文本 → 向量」这一件事：
- 不建索引
- 不做检索
- 不写数据库
- 不联网（模型已下载到本地缓存）

模型
----
BAAI/bge-small-zh-v1.5
- 参数量 24M，hidden size 512
- 许可证 MIT
- 模型文件 91 MB
- 实测单条编码 7.4 ms（i5-8265U，CPU）

为什么用 CLS pooling
--------------------
BGE 系列的官方用法是取 `[CLS]` token 的隐状态作为句向量，
然后做 L2 归一化。不是 mean pooling。

检索时用余弦相似度（等价于归一化后的内积）。
BGE 官方建议对**查询**加前缀 `为这个句子生成表示以用于检索相关文章：`，
文档不加前缀。本模块提供 `encode_query` / `encode_documents` 区分。

依赖
----
torch (CPU) + transformers。这是 04.9 之后唯一的重型依赖，
在 ADR-006 中单独决策。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Sequence

# 必须在 import torch 前设置，否则 HF 会写到用户目录
_DEFAULT_CACHE = Path(__file__).resolve().parents[3] / "storage" / "models"
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512
MAX_TOKENS = 512

# BGE 官方建议的查询前缀（只加在 query 侧）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingError(RuntimeError):
    """编码失败。"""


class BGEEmbedder:
    """BGE-small-zh-v1.5 编码器。

    惰性加载：第一次调用 `encode` 时才加载模型，
    避免仅 import 就占用 90MB 内存。

    线程安全：`encode` 内部只做前向推理，PyTorch 的推理在多线程下
    需要 `torch.no_grad()` 且不共享可变状态。本类只读模型权重，
    可安全地在多个线程中调用（每次调用独立构造 tensor）。
    """

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        *,
        cache_dir: str | Path | None = None,
        device: str = "cpu",
        batch_size: int = 16,
        revision: str | None = None,
        local_files_only: bool = True,
    ):
        """构造编码器。

        Args:
            model_name: HuggingFace 模型名（仅内部固定值，不接受外部任意路径）
            cache_dir: 模型缓存目录，默认项目内 storage/models
            device: 推理设备，默认 CPU
            batch_size: 批大小
            revision: 模型 revision。None 表示使用缓存中的默认分支
            local_files_only: **默认 True**，只用本地缓存，绝不访问外网。
                缺少模型时直接报错并提示如何准备，不自动下载。
        """

        self.model_name = model_name
        self.cache_dir = str(cache_dir) if cache_dir else None
        self.device = device
        self.batch_size = max(1, batch_size)
        self.revision = revision
        self.local_files_only = local_files_only

        self._tokenizer = None
        self._model = None
        self._resolved_commit: str = ""
        self._resolved_path: str = ""
        self._tokenizer_fingerprint: str = ""

    # ---- 惰性加载 ----

    def _ensure_loaded(self):
        if self._model is not None:
            return

        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover
            raise EmbeddingError(
                "本地 Embedding 需要 torch 与 transformers，"
                "请执行 pip install -r requirements-embedding.txt"
            ) from exc

        load_kwargs = {
            "cache_dir": self.cache_dir,
            "local_files_only": self.local_files_only,
        }
        if self.revision:
            load_kwargs["revision"] = self.revision

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, **load_kwargs
            )
            model = AutoModel.from_pretrained(self.model_name, **load_kwargs)
            model.eval()
            model.to(self.device)
            self._model = model
        except OSError as exc:
            # 离线模式下缺模型时的明确提示，不尝试联网
            raise EmbeddingError(
                f"本地模型不可用（local_files_only=True，不会自动下载）："
                f"{self.model_name}。请先在有网环境执行 "
                f"`python -c \"from transformers import AutoModel; "
                f"AutoModel.from_pretrained('{self.model_name}')\"` "
                f"以准备缓存。原始错误：{exc}"
            ) from exc
        except Exception as exc:
            raise EmbeddingError(f"模型加载失败：{exc}") from exc

        self._torch = torch
        self._resolve_identity()

    def _resolve_identity(self) -> None:
        """解析实际加载的模型 commit、路径与 tokenizer 指纹。

        这是模型一致性校验的数据来源：索引里记录的是构建时的
        真实 commit 与 tokenizer 指纹，查询时必须一致才能比较向量。

        注意：新版 transformers 不再在 config 上设置 `_commit_hash`，
        因此改用 `huggingface_hub.snapshot_download(local_files_only=True)`
        解析本地缓存实际对应的 commit。该调用只读本地缓存，不联网。
        """

        commit = ""
        resolved_path = ""

        # 方式一：huggingface_hub 解析本地快照路径（最可靠）
        try:
            from huggingface_hub import snapshot_download  # noqa: PLC0415

            kwargs = {"local_files_only": True}
            if self.revision:
                kwargs["revision"] = self.revision
            resolved_path = snapshot_download(self.model_name, **kwargs)
            # 快照目录名即 commit hash
            commit = Path(resolved_path).name
        except Exception:  # pragma: no cover - 缓存缺失或非 HF 布局
            commit = ""
            resolved_path = ""

        # 方式二：回退到 config 的 _commit_hash（旧版 transformers）
        if not commit:
            try:
                config = getattr(self._model, "config", None)
                if config is not None:
                    commit = str(
                        getattr(config, "_commit_hash", "") or ""
                    )
            except Exception:  # pragma: no cover
                commit = ""

        self._resolved_commit = commit
        self._resolved_path = resolved_path

        # tokenizer 指纹：词表大小 + 特殊 token id + 模型名
        # 这些字段变化意味着 token 切分可能不同，向量不能互比
        try:
            tok = self._tokenizer
            self._tokenizer_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "vocab_size": int(getattr(tok, "vocab_size", 0) or 0),
                        "cls_id": tok.cls_token_id,
                        "sep_id": tok.sep_token_id,
                        "pad_id": tok.pad_token_id,
                        "unk_id": tok.unk_token_id,
                        "model_max_length": int(
                            getattr(tok, "model_max_length", 0) or 0
                        ),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        except Exception:  # pragma: no cover
            self._tokenizer_fingerprint = ""

    # ---- 编码 ----

    def token_lengths(self, texts: Sequence[str]) -> list[int]:
        """返回每段文本的 token 长度（不含 padding）。

        用于入库前的长度校验，避免超长文本被静默截断。
        """

        if not texts:
            return []

        self._ensure_loaded()
        lengths = []
        for text in texts:
            encoded = self._tokenizer(
                text,
                add_special_tokens=True,
                truncation=False,
                return_tensors=None,
            )
            lengths.append(len(encoded["input_ids"]))
        return lengths

    def check_lengths(
        self,
        texts: Sequence[str],
        *,
        max_tokens: int = MAX_TOKENS,
    ) -> list[dict]:
        """校验 token 长度，返回超限明细。

        不抛异常，由调用方决定如何处理（拒绝 / 分片 / 记录）。
        """

        lengths = self.token_lengths(texts)
        over = [
            {"index": index, "tokens": tokens, "limit": max_tokens}
            for index, tokens in enumerate(lengths)
            if tokens > max_tokens
        ]
        return over

    def encode(
        self,
        texts: Sequence[str],
        *,
        as_query: bool = False,
        strict_length: bool = True,
    ) -> list[list[float]]:
        """把文本编码为归一化向量。

        Args:
            texts: 待编码文本。空列表直接返回空。
            as_query: 是否按“查询”处理（加 BGE 官方前缀）。
            strict_length: 是否禁止静默截断。默认 True。

        Returns:
            与输入等长的向量列表，每个向量 512 维、L2 范数为 1。

        Raises:
            EmbeddingError: 文本超过模型上限且 strict_length=True 时。
        """

        texts = [t for t in texts]
        if not texts:
            return []

        for index, text in enumerate(texts):
            if not isinstance(text, str):
                raise EmbeddingError(f"第 {index} 条不是字符串")

        if as_query:
            texts = [QUERY_INSTRUCTION + text for text in texts]

        self._ensure_loaded()
        torch = self._torch

        # 长度校验：禁止静默截断
        if strict_length:
            over = self.check_lengths(texts)
            if over:
                detail = ", ".join(
                    f"#{item['index']}={item['tokens']}tokens"
                    for item in over[:3]
                )
                raise EmbeddingError(
                    f"{len(over)} 条文本超过模型上限 {MAX_TOKENS} tokens"
                    f"（{detail}），拒绝静默截断。"
                    f"请先分片或改用支持更长上下文的模型。"
                )

        vectors: list[list[float]] = []

        for start in range(0, len(texts), self.batch_size):
            batch_texts = texts[start : start + self.batch_size]
            try:
                batch = self._tokenizer(
                    batch_texts,
                    padding=True,
                    truncation=True,
                    max_length=MAX_TOKENS,
                    return_tensors="pt",
                )
                batch = {k: v.to(self.device) for k, v in batch.items()}

                with torch.no_grad():
                    output = self._model(**batch)

                # BGE 用 [CLS] 向量
                embeddings = output.last_hidden_state[:, 0]
                embeddings = torch.nn.functional.normalize(
                    embeddings, p=2, dim=1
                )
                vectors.extend(embeddings.cpu().tolist())
            except Exception as exc:
                raise EmbeddingError(f"编码失败：{exc}") from exc

        return vectors

    def encode_query(self, text: str) -> list[float]:
        """编码单个查询。"""
        return self.encode([text], as_query=True)[0]

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """编码文档（不加查询前缀）。"""
        return self.encode(texts, as_query=False)

    # ---- 元信息 ----

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_fingerprint(self) -> dict:
        """模型指纹，用于索引与缓存的一致性校验。

        绑定实际加载的：
        - 模型名与 commit hash
        - tokenizer 指纹（词表大小 + 特殊 token id）
        - 池化方式、归一化方式
        - 编码规则（query 加指令 / document 不加）
        - 向量维度与最大 token 数

        **不允许仅凭维度相同就认定模型兼容** —— 不同 commit 的同一模型
        即使都是 512 维，向量空间也可能不同。

        若模型尚未加载，commit 与 tokenizer 指纹为空字符串，
        调用方（如索引构建）应视为「无法确认」而非「一致」。
        """

        commit = self._resolved_commit
        tokenizer_fp = self._tokenizer_fingerprint

        # 未加载时尝试解析本地缓存对应的 commit（只读本地，不联网）
        if not commit:
            try:
                from huggingface_hub import snapshot_download  # noqa: PLC0415

                kwargs = {"local_files_only": True}
                if self.revision:
                    kwargs["revision"] = self.revision
                path = snapshot_download(self.model_name, **kwargs)
                commit = Path(path).name
            except Exception:  # pragma: no cover - 缓存缺失
                commit = ""

        return {
            "model": self.model_name,
            "commit": commit,
            "tokenizer_fingerprint": tokenizer_fp,
            "dim": EMBEDDING_DIM,
            "max_tokens": MAX_TOKENS,
            "pooling": "cls_l2",
            "normalized": True,
            "query_instruction": QUERY_INSTRUCTION,
            "encode_mode_query": "query_with_instruction",
            "encode_mode_document": "document_no_instruction",
        }

    def describe(self) -> dict:
        return {
            "model": self.model_name,
            "dim": EMBEDDING_DIM,
            "max_tokens": MAX_TOKENS,
            "device": self.device,
            "batch_size": self.batch_size,
            "query_instruction": QUERY_INSTRUCTION,
            "loaded": self.loaded,
            "cache_dir": self.cache_dir or str(_DEFAULT_CACHE),
            "local_files_only": self.local_files_only,
            "revision": self.revision,
            "resolved_commit": self._resolved_commit,
            "tokenizer_fingerprint": self._tokenizer_fingerprint,
        }


def cosine_similarity(
    a: Sequence[float], b: Sequence[float]
) -> float:
    """两个已归一化向量的余弦相似度（即内积）。

    不做重归一化，调用方应保证输入来自 `BGEEmbedder.encode`。
    """

    if len(a) != len(b):
        raise EmbeddingError(
            f"向量维度不一致：{len(a)} vs {len(b)}"
        )
    return float(sum(x * y for x, y in zip(a, b)))


def rank_by_similarity(
    query_vector: Sequence[float],
    candidates: Sequence[tuple[str, Sequence[float]]],
    *,
    top_k: int = 10,
) -> list[tuple[str, float]]:
    """按相似度降序返回前 top_k 个候选。

    Args:
        query_vector: 查询向量
        candidates: [(标识, 向量)] 列表
        top_k: 返回条数

    Returns:
        [(标识, 相似度)]，按相似度降序
    """

    if top_k < 1:
        raise EmbeddingError("top_k 必须大于零")

    scored = [
        (key, cosine_similarity(query_vector, vector))
        for key, vector in candidates
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


__all__ = [
    "BGEEmbedder",
    "EMBEDDING_DIM",
    "EmbeddingError",
    "MAX_TOKENS",
    "MODEL_NAME",
    "QUERY_INSTRUCTION",
    "cosine_similarity",
    "rank_by_similarity",
]
