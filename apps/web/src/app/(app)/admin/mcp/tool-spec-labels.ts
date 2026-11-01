import type { MCPTool } from "@/lib/admin-types";
import { labelFor } from "@/lib/labels";

/**
 * MCP 헬스체크 도구의 노출 판정 문구 — 이 화면(`admin/mcp`)의 상태 확인 모달 전용(core-v0.20.0-upgrade §5.7).
 *
 * 거부 사유의 정본은 코어 `MCPToolSpecRejectReason`(`biznotes_core/services/agents/mcp_tool_spec.py`)이고, 생성 타입
 * `Schemas["MCPToolSpecOut"]["reason"]`(= `MCPTool["spec"]["reason"]`)의 null 아닌 값이다. 사전 키를 그 타입으로
 * 강제하므로 코어가 사유를 더해 생성 타입이 바뀌면 이 사전이 typecheck에서 실패한다. 문구는 계획 §5.7 그대로다.
 */
export type MCPToolRejectReason = NonNullable<MCPTool["spec"]["reason"]>;

export const MCP_TOOL_REJECT_REASON_LABEL: Record<MCPToolRejectReason, string> = {
  schema_not_object: "입력 스키마가 객체가 아닙니다",
  schema_too_deep: "입력 스키마가 너무 깊습니다",
  schema_too_large: "입력 스키마가 너무 큽니다",
  schema_structural_text: "입력 스키마에 허용되지 않는 글자가 있습니다",
  schema_unsupported_value: "입력 스키마에 지원하지 않는 값이 있습니다",
  // 코어 v0.21.0 #294 — 프로바이더 변환이 `$ref`를 펼친 크기·깊이가 예산을 넘는다(core-v0.22.0-upgrade AC-7)
  schema_expansion: "입력 스키마를 펼치면 너무 큽니다",
};

/**
 * 승인 스펙 고정 상태(코어 v0.21.0 #293, core-v0.22.0-upgrade AC-3). 정본은 헬스체크 응답 `approval`(제품
 * `app/services/mcp_pins.py`의 `PinState`)이고 사전 키를 생성 타입으로 강제한다. 코어는 `approved`만 노출한다 —
 * `unpinned`(업그레이드 전 승인 포함)·`changed`는 `scripts/mcp_approvals.py set TOOL=HASH`로 다시 승인해야 한다.
 */
export type MCPToolApproval = MCPTool["approval"];

export const MCP_TOOL_APPROVAL_LABEL: Record<NonNullable<MCPToolApproval>, string> = {
  approved: "승인됨",
  unpinned: "미고정 — 재승인 필요",
  changed: "승인 뒤 변경됨 — 재승인 필요",
  not_approved: "승인 없음",
};

/** 「설명 정화됨」 배지(`spec.status === "sanitized"`) — 정화 결과(설명 또는 스키마)가 원문과 다르다. */
export const MCP_TOOL_SANITIZED_LABEL = "설명 정화됨";

/**
 * 「노출 불가: <사유 라벨>」 배지 문구(`spec.status === "rejected"`). 배포 순서가 어긋나 웹이 모르는 사유가 오면
 * 사유 원문을 보이고(`labelFor` 미지 값 폴백), 사유가 없으면 「노출 불가」만 보인다.
 */
export function mcpToolRejectedLabel(reason: string | null | undefined): string {
  if (!reason) return "노출 불가";
  return `노출 불가: ${labelFor(MCP_TOOL_REJECT_REASON_LABEL, reason)}`;
}
