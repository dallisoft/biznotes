/**
 * 스튜디오 카드 레지스트리(mindmap-agent-skill AC-1.9, D-5, §8.1 `mindmap-artifact-binding` · #180).
 *
 * 카드마다 「어느 시스템 에이전트를 실행하는가」와 「어느 산출물이 그 카드의 결과인가」를 정한다.
 * 바인딩은 두 가지다.
 * - `skill`: 시스템 스킬 패키지(slug + visibility=system) — 실행은 그 wrapper 에이전트로 한다(plan §10 r5).
 * - `agent`: 시드 시스템 에이전트(이름 + kind + scope=system) — 보고서·브리핑·스터디 가이드·퀴즈·플래시카드·슬라이드 워크플로우.
 *   보고서·브리핑·스터디 가이드는 모든 소스를 반영하는 워크플로우로 옮겼다(#282 — 스킬 `input.mode: rag`는 코어 커버리지 옵션을
 *   받지 않는다). 옮기기 전 스킬 wrapper로 만든 옛 결과는 스튜디오 항목이 아니라 일반 결과 행으로 보인다(은퇴한 마인드맵 워크플로우와 같다).
 * 카드에 묶인 에이전트는 스킬·워크플로우 탭에 보이지 않고 스튜디오로만 연다(#180 결정 2 — D-23 「보임 + 다이얼로그」 대체).
 * 결과 목록의 스튜디오 항목은 산출물 이름이 아니라 run의 에이전트 id로 가린다(#180 결정 1) —
 * 사용자 스킬이 같은 이름(`quiz.json` 등)을 내도 스튜디오 항목이 되지 않는다.
 * 리터럴은 `apps/api/tests/test_studio_contract.py`가 시드 정의와 대조한다.
 */
import type { Agent, AgentKind, AgentRun, ArtifactRef } from "@/lib/agent-types";
import { MINDMAP_ARTIFACT_NAME } from "@/lib/artifact-viewers";
import type { Skill } from "@/lib/skill-types";

export type StudioBinding =
  | { type: "skill"; slug: string }
  | { type: "agent"; kind: AgentKind; name: string };

/**
 * 결과 항목 제목 규칙 — 마인드맵 `root.label` / 마크다운 첫 제목 / JSON 최상위 `title`(슬라이드 자료) /
 * 본문을 읽지 않음. 모두 실패하면 params.topic → 카드 이름.
 */
export type StudioTitleRule = "mindmap" | "markdown-heading" | "json-title" | "params";

export interface StudioCard {
  id: string;
  label: string;
  icon: string;
  description: string;
  /** 대표 산출물 — 결과 항목 제목과 상세 첫 화면은 이 산출물로 고른다. 산출물이 없는 loop 본문은 빈 문자열 */
  artifactName: string;
  binding: StudioBinding;
  title: StudioTitleRule;
  /**
   * 띠에 그리지 않는 카드 — 다른 카드의 앞 단계(슬라이드 자료의 개요 검토)나 하위 단계(삽화 loop 본문)를 맡는 에이전트.
   * 바인딩 맵에는 들어가므로 탭에서 숨고, 그 run은 이 카드의 결과 항목으로 그린다(slide-deck-studio D-3).
   * loop 본문은 부모 run 안에서 돌아 따로 run을 만들지 않는다.
   */
  hidden?: boolean;
}

export const MINDMAP_SKILL_SLUG = "mindmap";
/** 「슬라이드 자료」 카드와 그 개요 검토 단계(숨김 카드) — 다이얼로그·결과 항목이 id로 찾는다. */
export const SLIDE_DECK_CARD_ID = "slide-deck";
export const SLIDE_OUTLINE_CARD_ID = "slide-deck-outline";
/** 스킬 바인딩은 시스템 스킬만 — 시드 스킬은 seed_system_skill이 visibility=system으로 만든다 */
export const STUDIO_SKILL_VISIBILITY = "system";

export const STUDIO_CARDS: StudioCard[] = [
  {
    id: "mindmap",
    label: "마인드맵",
    icon: "🧠",
    description: "소스의 주제를 가지로 펼쳐 봅니다",
    artifactName: MINDMAP_ARTIFACT_NAME,
    binding: { type: "skill", slug: MINDMAP_SKILL_SLUG },
    title: "mindmap",
  },
  {
    id: "report",
    label: "보고서",
    icon: "📊",
    description: "소스를 바탕으로 구조화된 보고서를 씁니다",
    artifactName: "report.md",
    binding: { type: "agent", kind: "workflow", name: "보고서 생성" },
    title: "markdown-heading",
  },
  {
    id: "briefing",
    label: "브리핑 문서",
    icon: "📋",
    description: "핵심 요점을 한 장으로 간추립니다",
    artifactName: "briefing.md",
    binding: { type: "agent", kind: "workflow", name: "브리핑 문서" },
    title: "markdown-heading",
  },
  {
    id: "study-guide",
    label: "스터디 가이드",
    icon: "📖",
    description: "핵심 개념·용어·점검 질문으로 정리합니다",
    artifactName: "study-guide.md",
    binding: { type: "agent", kind: "workflow", name: "스터디 가이드" },
    title: "markdown-heading",
  },
  {
    id: "quiz",
    label: "퀴즈",
    icon: "❓",
    description: "객관식 문제를 풀고 채점합니다",
    artifactName: "quiz.json",
    binding: { type: "agent", kind: "workflow", name: "퀴즈 생성" },
    title: "params",
  },
  {
    id: "flashcards",
    label: "플래시카드",
    icon: "🗂️",
    description: "질문과 답 카드로 복습합니다",
    artifactName: "flashcards.json",
    binding: { type: "agent", kind: "workflow", name: "플래시카드 생성" },
    title: "params",
  },
  {
    id: "slide-deck",
    label: "슬라이드 자료",
    icon: "📽️",
    description: "자세한 자료나 발표용 슬라이드를 만듭니다",
    artifactName: "slide-deck.json",
    binding: { type: "agent", kind: "workflow", name: "슬라이드 자료" },
    title: "json-title",
  },
  {
    id: "slide-deck-outline",
    label: "슬라이드 자료 개요",
    icon: "📝",
    description: "슬라이드 구성을 미리 검토합니다",
    artifactName: "slide-outline.json",
    binding: { type: "agent", kind: "workflow", name: "슬라이드 자료 개요" },
    title: "json-title",
    hidden: true,
  },
  {
    id: "slide-deck-image",
    label: "슬라이드 삽화",
    icon: "🖼️",
    description: "슬라이드에 붙일 삽화를 그립니다",
    // 본문 이름 `slide-image-{params.item.slide}`의 한 예(test_studio_contract.py가 시드 이름 규칙과 대조)
    artifactName: "slide-image-1.png",
    binding: { type: "agent", kind: "workflow", name: "슬라이드 삽화" },
    title: "params",
    hidden: true,
  },
  {
    id: "slide-deck-write",
    label: "슬라이드 장 작성",
    icon: "🧾",
    description: "장마다 근거를 찾아 슬라이드를 씁니다",
    // 덱 loop 본문(#218) — 장 JSON을 loop 결과로 돌려줄 뿐 산출물을 저장하지 않는다
    artifactName: "",
    binding: { type: "agent", kind: "workflow", name: "슬라이드 장 작성" },
    title: "params",
    hidden: true,
  },
];

/** 띠에 그리는 카드 — 숨김 카드(앞 단계)는 뺀다. */
export const VISIBLE_STUDIO_CARDS = STUDIO_CARDS.filter((c) => !c.hidden);

export function studioCardById(id: string): StudioCard {
  const card = STUDIO_CARDS.find((c) => c.id === id);
  if (!card) throw new Error(`unknown studio card: ${id}`);
  return card;
}

/** 카드 바인딩을 푸는 목록 — 스킬 목록과 에이전트 목록(skill·workflow 조회 결과를 합친 것). */
export interface StudioCatalog {
  skills?: Skill[];
  agents?: Agent[];
}

/** 실행 가능한 파일 기반 스킬 → 실행 다이얼로그용 pseudo-Agent (wrapper agent_id로 실행) */
export function skillToAgent(skill: Skill): Agent {
  return {
    id: skill.wrapper_agent_id as string,
    name: skill.name,
    description: skill.description,
    icon: "🧩",
    kind: "skill",
    params_schema: skill.params_schema ?? { type: "object" },
    scope: skill.visibility === "system" ? "system" : "user",
    enabled: true,
    version: 1,
    input: skill.input,
  };
}

function runnable(skill: Skill): boolean {
  return skill.enabled !== false && skill.can_run === true && !!skill.wrapper_agent_id;
}

/** 카드가 실행할 에이전트 — 없거나 실행할 수 없으면 null(카드 비활성). */
export function findStudioAgent(card: StudioCard, catalog: StudioCatalog): Agent | null {
  const b = card.binding;
  if (b.type === "skill") {
    const skill = (catalog.skills ?? []).find(
      (s) => runnable(s) && s.slug === b.slug && s.visibility === STUDIO_SKILL_VISIBILITY,
    );
    return skill ? skillToAgent(skill) : null;
  }
  return (
    (catalog.agents ?? []).find(
      (a) => a.enabled !== false && a.scope === "system" && a.kind === b.kind && a.name === b.name,
    ) ?? null
  );
}

/** 에이전트 id → 그 에이전트를 실행하는 카드. 탭 숨김·다이얼로그·결과 항목 판정이 함께 쓴다. */
export function studioCardsByAgentId(catalog: StudioCatalog): Map<string, StudioCard> {
  const map = new Map<string, StudioCard>();
  for (const card of STUDIO_CARDS) {
    const agent = findStudioAgent(card, catalog);
    if (agent && !map.has(agent.id)) map.set(agent.id, card);
  }
  return map;
}

/** run에서 카드의 대표 산출물. 없으면 undefined. */
export function studioArtifact(run: AgentRun, card: StudioCard): ArtifactRef | undefined {
  return (run.artifact_keys ?? []).find((a) => a.name === card.artifactName);
}

/**
 * 결과 목록에서 스튜디오 항목으로 그릴 run인가 — 카드 에이전트의 성공 run ∧ 대표 산출물 있음 → 그 카드, 아니면 null.
 * 은퇴한 워크플로우 「마인드맵」의 옛 run은 카드 에이전트가 아니라 일반 행으로 보인다(산출물 칩은 이름으로 마인드맵 뷰어를 연다).
 */
export function studioCardForRun(
  run: AgentRun,
  byAgentId: Map<string, StudioCard>,
): StudioCard | null {
  if (run.status !== "succeeded") return null;
  const card = byAgentId.get(run.agent_id);
  return card && studioArtifact(run, card) ? card : null;
}
