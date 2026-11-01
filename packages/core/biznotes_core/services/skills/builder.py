"""SkillVersion → 런타임 노드 변환 + 실행 (skill_plan.md §9.3, §9.5).

builder는 "skill version → (nodes, edges)" 변환만 책임지고, 실행 루프·LangGraph·진행률 콜백은
기존 execute_agent()를 그대로 재사용한다. 스킬 노드는 kind_to_nodes에 넣지 않아 레거시와 격리.
"""

import json
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from biznotes_core.config import get_settings
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills.parser import parse_skill_md, validate_frontmatter

if TYPE_CHECKING:
    from biznotes_core.services.skills.report import PackageEvaluation

logger = structlog.get_logger()


class SkillRuntimeUnavailable(RuntimeError):
    """version의 실행 프로필을 이 배포가 실행할 수 없다 — legacy로 조용히 대체하지 않는다(D6)."""

# 권한 게이트(§9.3): instruction 모드는 constant·rag_search/source_load(+ `input.digest`의
# source_digest)·llm·json_validate·save_artifact만 생성한다.
_SKILL_PROMPT_SCAFFOLD = """<skill_instructions>
{skill.instructions}
</skill_instructions>

<references>
{skill.references}
</references>

<user_params>
{params}
</user_params>

<notebook_context>
{context}
</notebook_context>

Task: Execute the skill according to its instructions using the provided notebook context.
Respond in the same language as the instructions and context."""

# `input.digest`(#461)의 노트 컨텍스트 — 요약은 인용 번호가 없고, 근거 번호는 검색 발췌에만 있다
_DIGEST_CONTEXT = """<source_digests>
Summaries of every source in scope. They carry no citation numbers; do not cite them.
{digest.context}
</source_digests>

<source_excerpts>
Excerpts to cite with their [n] numbers.
{search.context}
</source_excerpts>"""

_DEFAULT_SYSTEM = (
    "You are executing a file-based Agent Skill. Follow the skill instructions precisely "
    "and ground your output only in the provided notebook context and user parameters."
)


class LoadedSkill(BaseModel):
    metadata_name: str
    metadata_description: str
    instructions: str  # SKILL.md body
    manifest: dict[str, Any]
    references: dict[str, str] = {}


def loaded_from_evaluation(evaluation: "PackageEvaluation", files: dict[str, bytes]) -> LoadedSkill:
    """패키지 평가 결과(report.py)에서 LoadedSkill을 만든다 — 같은 파싱 결과를 쓴다(D1)."""
    references: dict[str, str] = {}
    for path in evaluation.references:
        references[path] = files[path].decode("utf-8")
    return LoadedSkill(
        metadata_name=evaluation.name,
        metadata_description=evaluation.description,
        instructions=evaluation.body,
        # 원문 매니페스트를 넘긴다 — `build_nodes`가 기존 경로와 같은 입력을 받는다
        manifest=evaluation.raw_manifest,
        references=references,
    )


def load_skill_files(files: dict[str, bytes]) -> LoadedSkill:
    """패키지 파일 dict에서 SKILL.md/manifest/references를 로드 (§9.1)."""
    skill_md = files["SKILL.md"].decode("utf-8")
    fm, body = parse_skill_md(skill_md)
    name, description = validate_frontmatter(fm)

    manifest: dict[str, Any] = {}
    manifest_name = get_settings().skill_manifest_filename
    if manifest_name in files:
        manifest = json.loads(files[manifest_name].decode("utf-8"))
    else:
        # 빈 manifest로 진행한다(동작 유지). 파일명 설정이 어긋나면 권한 선언이 조용히
        # 사라지므로 신호를 남긴다(#152 B).
        logger.warning(
            "skill.manifest.missing",
            manifest_filename=manifest_name,
            file_count=len(files),
            files=sorted(files)[:20],
        )

    references: dict[str, str] = {}
    for path, data in files.items():
        if path.startswith("references/"):
            try:
                references[path] = data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("skill.reference.undecodable", path=path)
                continue
    return LoadedSkill(
        metadata_name=name,
        metadata_description=description,
        instructions=body,
        manifest=manifest,
        references=references,
    )


def _references_block(references: dict[str, str]) -> str:
    if not references:
        return "(none)"
    parts = []
    for path in sorted(references):
        parts.append(f"### {path}\n{references[path]}")
    return "\n\n".join(parts)


def build_nodes(loaded: LoadedSkill) -> tuple[list[dict], list[list[str]]]:
    """instruction 모드 SkillVersion → (nodes, edges). 권한 게이트 적용(§9.3)."""
    from biznotes_core.services.skills.validator import validate_manifest

    # 실행 경로는 `artifact` 위반을 경고만 한다 — 이미 게시된 버전 보존(#302 1단계 호환)
    manifest = validate_manifest(loaded.manifest, strict=False)
    perms = manifest["permissions"]
    artifact = manifest.get("artifact") or {}
    inp = manifest["input"]
    mode = inp["mode"]
    # permissions.rag = "노트 접근" 게이트: rag/full 둘 다 노트 데이터 접근(이름 유지, 의미 재사용).
    note_access = perms.get("rag", True)

    nodes: list[dict] = [
        {
            "id": "skill",
            "type": "constant",
            "value": {
                "instructions": loaded.instructions,
                "references": _references_block(loaded.references),
            },
        }
    ]
    edges: list[list[str]] = []
    prev = "skill"
    context_var = "(노트 컨텍스트 없음)"  # mode:none 기본

    if mode == "rag" and note_access:
        search: dict[str, Any] = {
            "id": "search",
            "type": "rag_search",
            "query": manifest.get("rag_query", "{params.topic}"),
            # 생략은 생략으로 남긴다 — 노드가 어드민 RAG 설정으로 해석한다(#45)
            "top_k": manifest.get("top_k"),
        }
        # 커버리지 옵션(#461)은 둔 것만 싣는다 — 생략하면 노드·실행 로그 입력이 지금과 같다
        for key in ("coverage", "per_source_limit"):
            if key in inp:
                search[key] = inp[key]
        context_var = "{search.context}"
        if inp.get("digest"):
            # 범위의 모든 소스를 덮는 요약을 검색 앞에 둔다(#461). 인용·근거표는 검색이 맡는다
            nodes.append({"id": "digest", "type": "source_digest"})
            edges.append([prev, "digest"])
            prev = "digest"
            context_var = _DIGEST_CONTEXT
        nodes.append(search)
        edges.append([prev, "search"])
        prev = "search"
    elif mode == "full" and note_access:
        nodes.append(
            {
                "id": "load",
                "type": "source_load",
                "max_sources": inp["max_sources"],
                "max_chars": inp["max_chars"],
            }
        )
        edges.append([prev, "load"])
        prev = "load"
        context_var = "{load.context}"

    generate = {
        "id": "generate",
        "type": "llm",
        "system": manifest.get("system", _DEFAULT_SYSTEM),
        # 컨텍스트 변수는 mode에 따라 build 시점에 치환(rag→search, full→load, none→없음).
        "prompt": _SKILL_PROMPT_SCAFFOLD.replace("{context}", context_var),
    }
    nodes.append(generate)
    edges.append([prev, "generate"])

    content, last = "{generate.text}", "generate"
    # 구조화 산출물 선언(#302) — 워크플로우와 같은 json_validate(재시도 1회, 재실패 시 run 실패,
    # 마인드맵 노드 상한)를 끼운다. 재시도는 generate와 같은 프롬프트·system으로 한다.
    # 엄격 검증을 통과한 블록에만 `schema`가 남는다(`validator._resolve_artifact`)
    schema = artifact.get("schema") if isinstance(artifact, dict) else None
    if schema:
        nodes.append(
            {
                "id": "validate",
                "type": "json_validate",
                "schema": schema,
                "source": "{generate.text}",
                "prompt": generate["prompt"],
                "system": generate["system"],
            }
        )
        edges.append(["generate", "validate"])
        content, last = "{validate.text}", "validate"

    nodes.append(
        {
            "id": "save",
            "type": "save_artifact",
            "name": artifact.get("name", "result.md"),
            "content": content,
            "content_type": artifact.get("content_type", "text/markdown"),
        }
    )
    edges.append([last, "save"])
    return nodes, edges


def legacy_run_definition(loaded: LoadedSkill) -> dict[str, Any]:
    """레거시 스킬의 실행 정의 — `build_nodes`에 채팅 출력 지정을 더한다.

    구조화 산출물(`artifact.schema`, #302)이면 채팅 본문·run 요약을 `validate` 노드의 검증본으로
    둔다. 지정하지 않으면 마지막 llm 노드(`generate`)의 첫 응답이 남아, 재시도 전의 깨진 JSON이나
    해석 전의 근거 번호가 채팅·요약에 보인다. 레거시 `generate`는 원래 출력에 사용 모델을 싣지
    않으므로 잃는 것이 없다.
    """
    nodes, edges = build_nodes(loaded)
    definition: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if any(node["id"] == "validate" for node in nodes):
        definition["chat_output"] = {"node_id": "validate"}
    return definition


def build_run_snapshot(version_id: uuid.UUID, package_hash: str, loaded: LoadedSkill) -> dict:
    """실행 재현성 스냅샷 (§9.4)."""
    return {
        "skill_version_id": str(version_id),
        "package_hash": package_hash,
        "name": loaded.metadata_name,
        "description": loaded.metadata_description,
        "manifest": loaded.manifest,
    }


async def execute_skill_package(
    ctx: Any,
    skill_version_id: uuid.UUID,
    *,
    resume_checkpoint: dict[str, Any] | None = None,
) -> str:
    """SkillVersion을 실행한다 — 실행 프로필로 경로를 고른다(§9.5, 표준 계획 D6).

    - `legacy_instruction_v1`: 패키지를 로드해 노드를 만들고 기존 `execute_agent()`로 실행한다.
    - `standard_agent_v1`: 스킬을 run에 고정하고 react 노드 하나로 실행한다(D6-6). 플래그가
      꺼져 있으면 명시적 오류다 — legacy 빌더로 조용히 대체하지 않는다.

    `resume_checkpoint`는 승인 재개 checkpoint(워커가 승인 결과를 넣은 것)다. 표준 version만
    승인 대기로 멈출 수 있으므로 재개도 이 경로로 들어온다 — 워커가 스킬 wrapper의 빈 정의로
    `execute_agent`를 부르면 구 prompt 경로에서 실패한다(`'prompt_template'`, D6 재개).

    깊이 0 호출자(직접 실행)는 AgentRun 워커여야 한다 — 채팅 진입 run도 같다(#86). 승인이
    필요하면 빈 문자열을 반환하고 `ctx.pending_approval`을 채우므로, 호출자가 그것을 영속화하고
    재개를 `resume_checkpoint`로 보내야 한다. 승인·재개가 없는 in-process 채팅에서 부르지 않는다.
    """
    import anyio

    from biznotes_core.models import SkillVersion
    from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD
    from biznotes_core.services.agents.runtime import execute_agent

    version = await ctx.db.get(SkillVersion, skill_version_id)
    if version is None:
        raise SkillRuntimeUnavailable(f"스킬 version을 찾을 수 없습니다: {skill_version_id}")
    if version.runtime_profile == SKILL_RUNTIME_STANDARD:
        return await _execute_standard(ctx, version, resume_checkpoint)
    if resume_checkpoint is not None:
        # legacy 파이프라인에는 승인 게이트가 있는 노드가 없다 — 재개할 대상이 없다
        raise SkillRuntimeUnavailable("legacy 스킬 version은 승인 재개를 지원하지 않습니다")
    files = await anyio.to_thread.run_sync(pkg.load_version_files, skill_version_id)
    loaded = load_skill_files(files)
    return await execute_agent(ctx, "workflow", legacy_run_definition(loaded))


async def _execute_standard(
    ctx: Any, version: Any, resume_checkpoint: dict[str, Any] | None
) -> str:
    from biznotes_core.services.agents.react.approval import CheckpointKeys
    from biznotes_core.services.agents.runtime import execute_agent
    from biznotes_core.services.skills import files as skill_files
    from biznotes_core.services.skills import runtime as skill_runtime
    from biznotes_core.services.skills import stages as skill_stages

    if not skill_runtime.runtime_enabled():
        raise SkillRuntimeUnavailable(
            "이 스킬 version은 표준 런타임(standard_agent_v1)으로만 실행됩니다. "
            "이 배포에서는 표준 런타임이 꺼져 있습니다"
        )
    name, _ = skill_runtime.frontmatter_label(version)
    if not name:
        raise SkillRuntimeUnavailable("표준 스킬 version에 name이 없습니다")
    # run staging이 D8 규칙으로 정한 version을 고정한다 — 카탈로그를 거치지 않는다(S-HCI-5)
    ctx.skill_pins[name] = str(version.id)

    # [#202] 다단계 선언 — 영속 메타데이터에서 읽는다. 실행 경로도 다시 검증한다
    # (가져오기 우회 경로로 들어온 행을 방어).
    stages = skill_stages.parse_stages(dict(getattr(version, "standard_metadata", None) or {}))
    if not stages:
        # 선언이 없는 스킬은 **오늘의 정의 경로**를 그대로 탄다 — 단일 단계 조립 함수를
        # 거치지 않아 기존 심볼이 그대로 살아 있다(테스트·외부 도구가 monkeypatch한다)
        definition = skill_runtime.standard_run_definition(name)
    else:
        # 지침 본문은 실행당 **한 번** 읽고 각 단계에 주입한다. 단계마다 읽으면
        # skill_read_run_budget_bytes를 단계 수만큼 먹는다.
        try:
            _, data = await skill_files.read_version_file(
                version,
                "SKILL.md",
                max_bytes=get_settings().skill_read_max_bytes,
                budget_used=ctx.skill_read_bytes[0],
                budget_limit=get_settings().skill_read_run_budget_bytes,
            )
            skill_body = data.decode("utf-8")
        except (skill_files.SkillFileAccessError, UnicodeDecodeError) as e:
            raise SkillRuntimeUnavailable(f"스킬 지침을 읽을 수 없습니다: {e}") from e
        ctx.skill_read_bytes[0] += len(data)
        definition = skill_runtime.staged_run_definition(
            stages, skill_name=name, skill_body=skill_body
        )
    if resume_checkpoint is None:
        return await execute_agent(ctx, "workflow", definition)
    # 워커가 승인을 소비해 넘긴 checkpoint가 재개의 신뢰 앵커다(#91). 값이 없으면 덮어쓰지 않는다.
    ctx.resume_checkpoint = resume_checkpoint
    ctx.resume_consumed = [False]
    node_id = resume_checkpoint.get(CheckpointKeys.REACT_NODE_ID)
    for node in definition["nodes"]:
        if node["id"] == node_id:
            node["_resume"] = resume_checkpoint
            break
    else:
        raise SkillRuntimeUnavailable(f"재개할 노드를 찾을 수 없습니다: {node_id}")
    return await execute_agent(
        ctx,
        "workflow",
        definition,
        resume_state=resume_checkpoint.get(CheckpointKeys.GRAPH_STATE) or {},
    )
