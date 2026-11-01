"""pgvector 열 타입 — `vector`(dense)·`sparsevec`(BM25 sparse).

차원을 고정하지 않는다(`vector`·`sparsevec`). 임베딩 공간(모델)마다 차원이 달라 한 열에
섞여 있고, 검색은 언제나 공간 하나로 거른 뒤 거리를 잰다(`services/vectorstore.py`).

값은 pgvector의 텍스트 형식으로 주고받는다 — asyncpg는 코덱이 없는 타입을 텍스트로 보낸다.
읽을 일은 드물다(검색은 거리만 계산한다). 그래서 별도 패키지 없이 여기서 변환한다.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType

#: sparse 차원 — pgvector `sparsevec`의 최대 차원(10억)이다. BM25 토큰 id(`abs(mmh3)`, 2³¹ 미만)는
#: 이 값으로 접어 넣는다. 접힌 id가 겹칠 확률은 무시할 만하고, 문서·질의를 같은 규칙으로 접으므로
#: 내적은 그대로다(겹치면 그 토큰 가중치가 합쳐질 뿐이다).
SPARSE_DIM = 1_000_000_000


class SparseLike(Protocol):
    @property
    def indices(self) -> Iterable[int]: ...

    @property
    def values(self) -> Iterable[float]: ...


def dense_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def sparse_literal(vector: SparseLike | Mapping[int, float]) -> str:
    """0부터 세는 토큰 id → pgvector 텍스트(`{i:v,...}/dim`, 1부터 센다). 접힌 id는 합친다."""
    if isinstance(vector, Mapping):
        pairs: Iterable[tuple[int, float]] = vector.items()
    else:
        pairs = zip(vector.indices, vector.values, strict=True)
    folded: dict[int, float] = {}
    for index, value in pairs:
        key = int(index) % SPARSE_DIM
        folded[key] = folded.get(key, 0.0) + float(value)
    body = ",".join(f"{k + 1}:{v!r}" for k, v in sorted(folded.items()) if v != 0.0)
    return "{" + body + "}/" + str(SPARSE_DIM)


def _parse_dense(text: str) -> list[float]:
    inner = text.strip()[1:-1]
    return [float(x) for x in inner.split(",")] if inner else []


def _parse_sparse(text: str) -> dict[int, float]:
    body = text.strip().split("/", 1)[0][1:-1]
    out: dict[int, float] = {}
    for item in filter(None, body.split(",")):
        k, v = item.split(":")
        out[int(k) - 1] = float(v)
    return out


class Vector(UserDefinedType[list[float]]):
    """차원 없는 `vector` — 값은 실수 목록."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Any) -> Callable[[Any], str | None]:
        def process(value: Any) -> str | None:
            if value is None or isinstance(value, str):
                return value
            return dense_literal(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Callable[[Any], list[float] | None]:
        def process(value: Any) -> list[float] | None:
            return None if value is None else _parse_dense(value)

        return process


class SparseVec(UserDefinedType[dict[int, float]]):
    """차원 없는 `sparsevec` — 값은 `{0부터 센 id: 가중치}`. `indices`·`values` 객체도 쓴다."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "sparsevec"

    def bind_processor(self, dialect: Any) -> Callable[[Any], str | None]:
        def process(value: Any) -> str | None:
            if value is None or isinstance(value, str):
                return value
            return sparse_literal(value)

        return process

    def result_processor(
        self, dialect: Any, coltype: Any
    ) -> Callable[[Any], dict[int, float] | None]:
        def process(value: Any) -> dict[int, float] | None:
            return None if value is None else _parse_sparse(value)

        return process


# 반영(alembic check)이 열 타입을 알아보게 한다 — 모르면 NullType으로 읽고 경고한다
ischema_names["vector"] = Vector
ischema_names["sparsevec"] = SparseVec
