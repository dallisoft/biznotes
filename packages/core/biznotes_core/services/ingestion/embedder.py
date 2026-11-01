"""로컬 임베딩 — fastembed (API 키 불필요, 폐쇄망 동작).

dense는 어드민에 임베딩 모델이 없을 때의 기본값이다. 어드민 기본 임베딩 모델로의
라우팅과 공간(모델·컬렉션) 구분은 `services/embedding_spaces.py`가 맡는다(#7).
sparse(BM25)는 모델과 무관하게 항상 여기서 만든다. 인용 검증의 문장 유사도
(`rag_chat.verify_citations`)도 색인과 무관한 계산이라 로컬 dense를 쓴다.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from fastembed import SparseTextEmbedding, TextEmbedding

# 한국어 포함 다국어 지원 384차원 모델
DEFAULT_DENSE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 384

_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH")  # None이면 fastembed 기본값


@dataclass
class SparseVector:
    indices: list[int]
    values: list[float]


@lru_cache
def _dense_model() -> TextEmbedding:
    return TextEmbedding(model_name=DEFAULT_DENSE_MODEL, cache_dir=_CACHE_DIR)


@lru_cache
def _sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(model_name=DEFAULT_SPARSE_MODEL, cache_dir=_CACHE_DIR)


def embed_dense(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in _dense_model().embed(texts, batch_size=16)]


def embed_sparse(texts: list[str]) -> list[SparseVector]:
    return [
        SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
        for e in _sparse_model().embed(texts, batch_size=16)
    ]


def embed_query_dense(text: str) -> list[float]:
    return embed_dense([text])[0]


def embed_query_sparse(text: str) -> SparseVector:
    return embed_sparse([text])[0]
