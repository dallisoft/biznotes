import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

export type AgentKind = Schemas["AgentOut"]["kind"];
export type JsonSchemaProperty = Schemas["JsonSchemaProperty"];
export type ParamsSchema = Schemas["ParamsSchema"];

/** 입력 소스 정책 — 검색 모드 × 소스 스코프 (agent-input-source-plan.md) */
export type AgentInput = Schemas["AgentInput"];
export type InputMode = AgentInput["mode"];
export type InputScope = AgentInput["scope"];

export type Agent = Schemas["AgentOut"];

export const INPUT_MODE_LABEL: Record<InputMode, string> = {
  rag: "검색",
  full: "전문",
  none: "입력 없음",
};

export const INPUT_SCOPE_LABEL: Record<InputScope, string> = {
  all: "노트 전체",
  selected: "선택 소스",
};

export interface ArtifactRef {
  key: string;
  name: string;
  content_type: string;
}

export type AgentRun = Schemas["AgentRunOut"];

/** 「실패한 결과 모두 숨기기」 응답 — 숨긴 수와 「되돌리기」용 run id(#220) */
export type HiddenRuns = Schemas["HiddenRunsOut"];

/** HITL 승인 요청 — 부작용 도구 실행 전 사용자 승인/거부 대상(phase-19-design.md §1.1) */
export type AgentRunApproval = Schemas["AgentRunApprovalOut"];

export const APPROVAL_STATUS_LABEL: Record<AgentRunApproval["status"], string> = {
  pending: "승인 대기",
  approved: "승인됨",
  rejected: "거부됨",
  expired: "만료됨",
};

/** 노드별 실행 로그 — react_agent의 tool-call 보조행(node_type="react_tool_call")도
 * 포함해 반환된다(ReAct Phase 2 Q5, phase-19-design.md §5.2). */
export interface ExecutionLog {
  id: string;
  run_id: string;
  node_id: string;
  node_type: string;
  status: string;
  error_message: string | null;
  /** 노드 실패 코드(코어 v0.19.0 #216) — 문구는 `node-error-labels.ts`의 `nodeErrorLabel`로 고른다. 성공 행은 null. */
  error_code: string | null;
  token_usage: Record<string, unknown> | null;
  cost_usd: number | null;
  duration_ms: number | null;
  started_at: string | null;
  finished_at: string | null;
  /** 실제로 응답한 모델(제품 #216, 코어 #359) — 성공한 llm·react_agent 행만. 그 밖은 null. */
  model: Schemas["NodeModelOut"] | null;
  /** 1차 모델이 실패해 폴백 체인의 다른 모델이 응답했는지 — `model`과 같은 행만. 그 밖은 null. */
  fallback_used: boolean | null;
  /** 모든 후보가 실패한 행이 시도한 모델(순서대로, 실패 코드 포함). 그 밖은 null. */
  model_attempts: Schemas["NodeModelAttemptOut"][] | null;
}

export const REACT_TOOL_CALL_NODE_TYPE = "react_tool_call";

/** 도구별 side_effect 분류 + 이를 자동 승인하는 workflow agent 목록(어드민, 19c) */
export interface AutoApprovalRef {
  agent_id: string;
  agent_name: string;
  node_id: string;
  policy: Record<string, unknown>;
}

export interface ToolPolicy {
  tool_name: string;
  side_effect: boolean;
  auto_approved_by: AutoApprovalRef[];
}

export const KIND_LABEL: Record<AgentKind, string> = {
  workflow: "워크플로우",
  skill: "스킬",
  plugin: "플러그인",
  tool: "도구",
  mcp: "MCP",
};

export const RUN_STATUS_LABEL: Record<AgentRun["status"], string> = {
  queued: "대기",
  running: "실행 중",
  succeeded: "완료",
  failed: "실패",
  cancelled: "취소됨",
  awaiting_approval: "승인 대기",
};

// 조회를 전역(total)으로 만들어 미지의 값(옛 판이 남긴 trigger 등)이 와도 빈 배지가 뜨지 않게 한다.
/** 백엔드 TRIGGER_SOURCES(biznotes_core/models/agent.py) 정본 — `AgentRun["trigger"]`와 같다. */
type KnownTrigger = AgentRun["trigger"];

// `satisfies`로 정본 키 누락을 컴파일 타임에 잡되, 조회는 string으로 받아 런타임 전역성을 유지한다.
const TRIGGER_LABEL_MAP = {
  manual: "수동",
  chat: "채팅",
} satisfies Record<KnownTrigger, string>;

/** 알 수 없는 트리거에도 안전한 라벨 조회 — 빈 배지 렌더 방지. */
export function triggerLabel(trigger: string): string {
  return (TRIGGER_LABEL_MAP as Record<string, string>)[trigger] ?? trigger;
}

