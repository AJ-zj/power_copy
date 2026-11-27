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
import re
from threading import Lock
from typing import Sequence

# 必须在 import torch 前设置，否则 HF 会写到用户目录
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "storage" / "models"
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
        self._load_lock = Lock()

    # ---- 惰性加载 ----

    def _ensure_loaded(self):
        with self._load_lock:
            if self._model is None:
                self._load()

    def _load(self):
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover
            raise EmbeddingError(
                "本地 Embedding 需要 torch 与 transformers，"
                "请执行 pip install -r requirements-embedding.txt"
            ) from exc

        try:
            snapshot = Path(snapshot_download(
                self.model_name,
                revision=self.revision,
                cache_dir=self.cache_dir,
                local_files_only=self.local_files_only,
                allow_patterns=["*.json", "vocab.txt", "*.safetensors"],
            ))
            commit = snapshot.name
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise EmbeddingError("无法确认本地模型快照的提交版本。")

            # Both components load from the same resolved snapshot, even if the branch
            # reference or default cache changes while this encoder is in use.
            tokenizer = AutoTokenizer.from_pretrained(
                snapshot, local_files_only=True, trust_remote_code=False
            )
            model = AutoModel.from_pretrained(
                snapshot, local_files_only=True, trust_remote_code=False, use_safetensors=True
            )
            model.eval()
            model.to(self.device)
            tokenizer_fingerprint = self._fingerprint_tokenizer(tokenizer)
        except OSError as exc:
            raise EmbeddingError(
                "本地模型不可用，请确认所选版本已完整写入模型缓存。"
            ) from exc
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError("本地模型加载失败，请检查模型缓存和依赖。") from exc

        self._torch = torch
        self._tokenizer = tokenizer
        self._resolved_commit = commit
        self._resolved_path = str(snapshot)
        self._tokenizer_fingerprint = tokenizer_fingerprint
        self._model = model

    @staticmethod
    def _fingerprint_tokenizer(tokenizer) -> str:
        """Bind the actual fast tokenizer, including vocabulary and processing rules."""
        backend = getattr(tokenizer, "backend_tokenizer", None)
        if backend is None:
            raise EmbeddingError("无法确认分词器内容。")
        content = {
            "backend": json.loads(backend.to_str()),
            "special_tokens": tokenizer.special_tokens_map,
            "padding_side": tokenizer.padding_side,
            "truncation_side": tokenizer.truncation_side,
            "model_max_length": tokenizer.model_max_length,
        }
        return hashlib.sha256(json.dumps(
            content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

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
        - tokenizer 指纹（词表、分词规则及配置）
        - 池化方式、归一化方式
        - 编码规则（query 加指令 / document 不加）
        - 向量维度与最大 token 数

        **不允许仅凭维度相同就认定模型兼容** —— 不同 commit 的同一模型
        即使都是 512 维，向量空间也可能不同。

        若模型尚未加载，commit 与 tokenizer 指纹为空字符串，
        调用方（如索引构建）应视为「无法确认」而非「一致」。
        """

        return {
            "model": self.model_name,
            "commit": self._resolved_commit,
            "tokenizer_fingerprint": self._tokenizer_fingerprint,
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
