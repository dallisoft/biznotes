"""이미지 생성 노드 실행기 — `image_generate` (#338).

node: {prompt(필수), name?(기본 "image"), n?(기본 1), size? | aspect_ratio?, model_id?}

- 프롬프트는 `render_prompt`로 렌더한다. 이미지 API에는 system 메시지가 없어 비신뢰 데이터 규칙
  (`UNTRUSTED_DATA_RULE`)을 실을 곳이 없으므로, 렌더 결과에 `untrusted_data` 블록(소스·검색·도구
  본문, `|untrusted` 값, 태그를 품은 값)이 있으면 프로바이더를 부르기 전에 노드를 실패시킨다.
  비신뢰 본문은 규칙을 가진 앞 `llm` 노드가 그림 묘사로 바꾼 뒤 그 출력을 쓴다.
- 상한(모든 환경): 장수 1~`IMAGE_MAX_COUNT`, 크기 `IMAGE_SIZES`(또는 `IMAGE_ASPECT_RATIOS`),
  프롬프트 `IMAGE_PROMPT_MAX_CHARS`자, 산출물 장당 `IMAGE_MAX_BYTES`(20 MiB) — `gateway.image`.
- 산출물은 PNG·JPEG로만 저장한다. 확장자는 바이트로 정한 MIME에서 붙인다(`name`의 .png·.jpg·.jpeg는
  떼어 낸다). `n`이 2 이상이면 `<name>-1.png`처럼 1부터 번호를 붙인다. 이름에 경로 구분자·`..`·제어
  문자가 있으면 거부한다.
- 반환: {key(첫 장), keys, names, images: [{key, name, content_type, bytes}], count}.
  바이트는 상태에 넣지 않는다. 뒤 노드는 `{node_id.key}`·`{node_id.names}`로, 제품은 run
  산출물 이름으로 참조한다.
- 사용량 `ctx.usage["images"]`, 비용은 모델 `cost_per_1k.per_image` × 장수(없으면 응답 토큰 사용량
  × input/output 단가, 둘 다 없으면 0)를 `ctx.add_cost`로 남긴다.
"""

import re
import uuid
from typing import Any

from biznotes_core.config import get_settings
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.templating import render, render_prompt
from biznotes_core.services.untrusted import has_untrusted_block, strip_invisible

_DEFAULT_NAME = "image"
_NAME_MAX_CHARS = 200
_UNSAFE_NAME = re.compile(r"[/\\\x00-\x1f\x7f-\x9f]")
_IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g)$", re.IGNORECASE)
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}


def _int_count(raw: Any) -> int:
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AgentRuntimeError(f"image_generate n은 정수여야 합니다: {raw!r}")
    return raw


def _request_size(node: dict) -> str:
    from biznotes_core.services.gateway.image import IMAGE_ASPECT_RATIOS

    size, ratio = node.get("size"), node.get("aspect_ratio")
    if size is not None and ratio is not None:
        raise AgentRuntimeError("image_generate는 size와 aspect_ratio를 함께 받지 않습니다")
    if ratio is not None:
        if not isinstance(ratio, str) or ratio not in IMAGE_ASPECT_RATIOS:
            allowed = ", ".join(IMAGE_ASPECT_RATIOS)
            raise AgentRuntimeError(f"image_generate aspect_ratio는 {allowed} 중 하나여야 합니다")
        return IMAGE_ASPECT_RATIOS[ratio]
    if size is None:
        return "1024x1024"
    return str(size)  # 목록 검사는 게이트웨이 상한 검사가 한다


def _name_stem(raw: str) -> str:
    stem = _IMAGE_SUFFIX.sub("", raw.strip())
    if (
        not stem
        or stem.startswith(".")
        or ".." in stem
        or len(stem) > _NAME_MAX_CHARS
        or _UNSAFE_NAME.search(stem)
    ):
        raise AgentRuntimeError(f"image_generate name이 올바르지 않습니다: {raw[:80]!r}")
    return stem


def _model_id(node: dict) -> uuid.UUID | None:
    raw = node.get("model_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as e:
        raise AgentRuntimeError(f"image_generate model_id가 올바르지 않습니다: {raw}") from e


def _cost(usage: dict[str, int], cost_per_1k: dict[str, Any] | None, count: int) -> float:
    from biznotes_core.services.gateway.cost import compute_cost

    per_image = (cost_per_1k or {}).get("per_image")
    if per_image is not None:
        return float(per_image) * count
    return compute_cost(usage, cost_per_1k) if usage else 0.0


async def _exec_image_generate(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """템플릿 프롬프트 → 이미지 생성 → 바이너리 산출물 저장(모듈 docstring)."""
    import anyio

    from biznotes_core.services.gateway.image import check_image_request, generate_images
    from biznotes_core.services.gateway.llm import GatewayError, resolve_model
    from biznotes_core.services.storage import ensure_buckets, put_object

    if not node.get("prompt"):
        raise AgentRuntimeError("image_generate 노드에 prompt가 없습니다")
    prompt = strip_invisible(render_prompt(str(node["prompt"]), state))
    if has_untrusted_block(prompt):
        raise AgentRuntimeError(
            "image_generate 프롬프트에 소스·검색·도구 본문(비신뢰 데이터)을 직접 넣을 수 "
            "없습니다 — 앞 llm 노드로 그림 묘사를 만든 뒤 그 출력을 쓰세요"
        )
    n = _int_count(node.get("n"))
    size = _request_size(node)
    check_image_request(prompt, n, size)
    stem = _name_stem(render(str(node.get("name") or _DEFAULT_NAME), state))

    resolved = await resolve_model(ctx.db, _model_id(node), modality="image")
    if "image" not in (resolved.model.capabilities or []):
        raise GatewayError("요청한 모델은 이미지 생성(image) 모델이 아닙니다")
    result = await generate_images(resolved, prompt, n=n, size=size)

    # 사용량·비용은 생성 직후에 남긴다 — 저장이 실패해 재시도돼도 이미 낸 생성 비용이 빠지지 않는다
    count = len(result.images)
    ctx.usage["images"] = ctx.usage.get("images", 0) + count
    if result.usage:
        ctx.add_tokens(result.usage)
    ctx.add_cost(_cost(result.usage, resolved.model.cost_per_1k, count))

    saved: list[dict[str, Any]] = []
    for i, image in enumerate(result.images, start=1):
        ext = _EXTENSIONS[image.content_type]
        name = f"{stem}.{ext}" if n == 1 else f"{stem}-{i}.{ext}"
        saved.append(
            {
                "key": f"{ctx.run_id}/{name}",
                "name": name,
                "content_type": image.content_type,
                "bytes": len(image.data),
            }
        )

    def _put() -> None:
        ensure_buckets()
        bucket = get_settings().storage_bucket_artifacts
        for entry, image in zip(saved, result.images, strict=True):
            put_object(bucket, entry["key"], image.data, image.content_type)

    await anyio.to_thread.run_sync(_put)
    for entry in saved:
        ctx.artifact_keys.append(
            {"key": entry["key"], "name": entry["name"], "content_type": entry["content_type"]}
        )
    return {
        "key": saved[0]["key"],
        "keys": [e["key"] for e in saved],
        "names": [e["name"] for e in saved],
        "images": saved,
        "count": count,
    }
