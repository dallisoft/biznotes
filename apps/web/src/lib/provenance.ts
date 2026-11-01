import type { ChatEvidence } from "@/lib/chat-types";
import { NOTE_PROVENANCE_MODE_LABEL, type Note, type ProvenanceMode } from "@/lib/note-types";

/**
 * 채팅 evidence(SSE `done.evidence` / 영속화된 `ChatMessage.provenance.items`)와
 * 노트 `provenance`를 표시용 배지로 정규화하는 순수 어댑터.
 * 백엔드가 만든 값(services/provenance.py)을 그대로 신뢰하고 재판정하지 않는다.
 */

export type ProvenanceItem = ChatEvidence;

export const PROVENANCE_STATUS_LABEL: Record<ProvenanceItem["status"], string> = {
  succeeded: "완료",
  failed: "실패",
  blocked: "차단됨",
  unknown: "상태 미상",
};

function itemLabel(item: ProvenanceItem): string {
  switch (item.kind) {
    case "skill":
      return `스킬 ${item.name ?? "이름 없음"}${item.version != null ? ` v${item.version}` : ""}`;
    case "mcp":
      return `MCP ${item.server ?? "?"}/${item.tool ?? "?"}`;
    case "tool":
      return `도구 ${item.tool ?? "?"}${item.count != null ? ` (${item.count})` : ""}`;
    case "source":
      return `소스 ${item.title ?? item.source_id ?? "원문"}`;
    case "agent":
      return `에이전트 ${item.name ?? "?"}`;
    default:
      return "근거";
  }
}

export interface ProvenanceBadge {
  key: string;
  label: string;
  status: ProvenanceItem["status"];
  failed: boolean;
}

/** evidence/provenance 항목 하나를 배지로 정규화한다 — status가 succeeded가 아니면 라벨에 접미사를 붙인다. */
export function toProvenanceBadge(item: ProvenanceItem, index: number): ProvenanceBadge {
  const suffix = item.status === "succeeded" ? "" : ` · ${PROVENANCE_STATUS_LABEL[item.status]}`;
  return {
    key: `${item.kind}-${item.server ?? item.source_id ?? item.tool ?? item.name ?? index}-${index}`,
    label: `${itemLabel(item)}${suffix}`,
    status: item.status,
    failed: item.status === "failed",
  };
}

/**
 * `kind="tool"` 항목 중 `tool="run_skill"`/`mcp_`로 시작하는 것은, **성공한 호출이고**
 * 같은 배열에 짝이 되는 `kind="skill"`/`kind="mcp"` 항목이 **실제로 있을 때만**
 * 저장 계층의 의도된 중복이다(BL-17 — `services/provenance.py::build_chat_provenance`는
 * tool_evidence와 tool_calls를 각각 items에 담고, dedup은 실패한 mcp 호출에만
 * 적용된다). 실패·차단(blocked) 상태의 `run_skill`/mcp 호출이나, `origin=agent` 노트에서
 * 그 run 자체가 스킬 실행이 아니라 스킬을 *호출*만 한 경우(스킬 신원 불명 등 이유로
 * `kind="skill"` 항목이 아예 없는 경우)는 그 tool 항목이 **유일한 기록**이므로 접지
 * 않는다(phase-3-review.md P3-01). 화면에는 더 구체적인 skill/mcp 항목만 남기고
 * 접는다 — 저장 형태는 그대로 두는 "화면에서 접기" 정책(BL-17 처분).
 */
function isRedundantToolDuplicate(item: ProvenanceItem, siblings: ProvenanceItem[]): boolean {
  if (item.kind !== "tool" || item.status !== "succeeded") return false;
  if (item.tool === "run_skill") {
    return siblings.some((other) => other.kind === "skill");
  }
  if (item.tool?.startsWith("mcp_")) {
    return siblings.some((other) => other.kind === "mcp");
  }
  return false;
}

/** 표시용으로 접을 항목(BL-17 중복)을 제거한다 — 저장된 items 자체는 건드리지 않는다. */
export function filterDisplayableItems(
  items: ProvenanceItem[] | null | undefined,
): ProvenanceItem[] {
  const all = items ?? [];
  return all.filter((item) => !isRedundantToolDuplicate(item, all));
}

export function toProvenanceBadges(
  items: ProvenanceItem[] | null | undefined,
): ProvenanceBadge[] {
  return filterDisplayableItems(items).map(toProvenanceBadge);
}

export type NoteProvenanceState = "none" | "unrecorded" | "empty" | "items";

export interface NoteProvenanceView {
  state: NoteProvenanceState;
  modeLabel: string | null;
  badges: ProvenanceBadge[];
  /** 생성 후 편집됨 — provenance 상태와 독립적으로 함께 표시될 수 있다. */
  edited: boolean;
}

export const NOTE_PROVENANCE_COPY = {
  none: "생성 근거 기록 없음",
  empty: "사용 기록된 스킬·MCP·도구·소스 없음",
  unrecorded:
    "생성 근거 미기록 — 답변 생성 시점의 스킬·도구 기록이 없습니다(2026-09 기능 도입 이전 답변 또는 기록 실패). 본문은 채팅 원문에서 서버가 복사했습니다.",
  edited: "생성 후 편집됨 — 아래 근거는 생성 시점 기준",
};

function modeLabel(mode: ProvenanceMode): string {
  return NOTE_PROVENANCE_MODE_LABEL[mode];
}

/**
 * `origin != "manual"` 노트의 "생성 근거" 섹션 5상태를 계산한다(AC-12).
 * manual 노트는 호출부에서 아예 섹션을 렌더하지 않는다 — provenance는 항상 null이라
 * 여기서 상태를 만들어도 무의미하다.
 */
export function getNoteProvenanceView(
  note: Pick<Note, "provenance" | "content_intact">,
): NoteProvenanceView {
  const edited = note.content_intact === false;
  const provenance = note.provenance;
  if (!provenance) {
    return { state: "none", modeLabel: null, badges: [], edited };
  }
  if (provenance.mode === "unrecorded") {
    return { state: "unrecorded", modeLabel: modeLabel(provenance.mode), badges: [], edited };
  }
  const badges = toProvenanceBadges(provenance.items);
  if (badges.length === 0) {
    return { state: "empty", modeLabel: modeLabel(provenance.mode), badges: [], edited };
  }
  return { state: "items", modeLabel: modeLabel(provenance.mode), badges, edited };
}
