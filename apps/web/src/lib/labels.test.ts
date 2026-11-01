import { describe, expect, it, vi } from "vitest";
import { MODALITIES } from "@/components/admin/llm/capability-picker";
import { KIND_LABEL as AGENT_KIND_LABEL, RUN_STATUS_LABEL as AGENT_RUN_STATUS_LABEL } from "./agent-types";
import {
  ADMIN_PERMISSION_LABEL,
  activeLabel,
  AGENT_SCOPE_LABEL,
  agentScopeLabel,
  AUDIT_ACTION_LABEL,
  auditActionLabel,
  CAPABILITY_LABEL,
  capabilityLabel,
  describeShareTarget,
  FOREIGN_GROUP_LABEL,
  healthLabel,
  KIND_LABEL,
  kindLabel,
  labelFor,
  NOTEBOOK_PERMISSION_LABEL,
  notebookAccessLabel,
  notebookPermissionLabel,
  notebookRowPermissionLabel,
  ROLE_LABEL,
  roleLabel,
  RUN_STATUS_LABEL,
  runStatusLabel,
  SHARE_PERMISSION_OPTIONS,
  SKILL_STATUS_LABEL,
  SKILL_STATUS_TONE,
  skillStatusLabel,
  workflowStatusLabel,
} from "./labels";

describe("labelFor — 공통 조회 규칙 (AC-1.6)", () => {
  const dict = { a: "가" } as const;

  it("알려진 값은 라벨", () => {
    expect(labelFor(dict, "a")).toBe("가");
  });

  it("미지 값은 원시값 그대로", () => {
    expect(labelFor(dict, "zzz")).toBe("zzz");
  });

  it("프로토타입 멤버 이름도 미지 값으로 다룬다(함수·객체가 새지 않음)", () => {
    expect(labelFor(dict, "constructor")).toBe("constructor");
    expect(labelFor(dict, "__proto__")).toBe("__proto__");
    expect(labelFor(dict, "toString")).toBe("toString");
  });

  it("값이 없으면 「—」", () => {
    expect(labelFor(dict, null)).toBe("—");
    expect(labelFor(dict, undefined)).toBe("—");
    expect(labelFor(dict, "")).toBe("—");
  });
});

describe("역할·권한 (AC-1.6)", () => {
  it("ROLE_LABEL: admin → 관리자, user → 사용자, 미지 값 폴백", () => {
    expect(ROLE_LABEL).toEqual({ admin: "관리자", user: "사용자" });
    expect(roleLabel("admin")).toBe("관리자");
    expect(roleLabel("user")).toBe("사용자");
    expect(roleLabel("auditor")).toBe("auditor");
  });

  it("NOTEBOOK_PERMISSION_LABEL: 소유자·편집 가능·읽기 전용, 미지 값 폴백", () => {
    expect(notebookPermissionLabel("owner")).toBe("소유자");
    expect(notebookPermissionLabel("write")).toBe("편집 가능");
    expect(notebookPermissionLabel("read")).toBe("읽기 전용");
    expect(notebookPermissionLabel("comment")).toBe("comment");
  });

  it("목록 행 권한: 실제 소유자가 아닌 owner(관리자 승격)는 「관리자 권한」, 그 밖은 권한 라벨(core-v0.20.0 AC-1.9 ④)", () => {
    expect(ADMIN_PERMISSION_LABEL).toBe("관리자 권한");
    expect(notebookRowPermissionLabel({ is_owner: false, permission: "owner" })).toBe("관리자 권한");
    expect(notebookRowPermissionLabel({ is_owner: true, permission: "owner" })).toBe("소유자");
    expect(notebookRowPermissionLabel({ is_owner: false, permission: "write" })).toBe("편집 가능");
    expect(notebookRowPermissionLabel({ is_owner: false, permission: "read" })).toBe("읽기 전용");
    expect(notebookRowPermissionLabel({ is_owner: false, permission: "comment" })).toBe("comment");
  });

  it("괄호 영문 판 라벨 사전은 다시 export되지 않는다(#151)", async () => {
    const labels: Record<string, unknown> = await import("./labels");
    const bulk: Record<string, unknown> = await import("./notebook-bulk");
    expect(labels).not.toHaveProperty("SHARE_PERMISSION_LABELS");
    expect(bulk).not.toHaveProperty("SHARE_PERMISSION_LABELS");
  });
});

describe("상태 라벨 (AC-1.6)", () => {
  it("워크플로우: enabled → 게시됨, 아니면 초안", () => {
    expect(workflowStatusLabel(true)).toBe("게시됨");
    expect(workflowStatusLabel(false)).toBe("초안");
  });

  it("스킬: 초안·공개 중·검토 대기·공개 철회됨, 미지 값 폴백", () => {
    expect(SKILL_STATUS_LABEL).toEqual({
      draft: "초안",
      published: "공개 중",
      review_pending: "검토 대기",
      withdrawn: "공개 철회됨",
    });
    expect(skillStatusLabel("published")).toBe("공개 중");
    expect(skillStatusLabel("archived")).toBe("archived");
  });

  it("스킬 상태 톤: 사전의 모든 상태에 톤이 하나씩 있고, 「공개 중」은 success다(P6 — P-07 ③ 화면 간 통일)", () => {
    expect(SKILL_STATUS_TONE).toEqual({
      draft: "neutral",
      published: "success",
      review_pending: "info",
      withdrawn: "warning",
    });
    expect(Object.keys(SKILL_STATUS_TONE).sort()).toEqual(Object.keys(SKILL_STATUS_LABEL).sort());
  });

  it("활성: 활성 / 비활성", () => {
    expect(activeLabel(true)).toBe("활성");
    expect(activeLabel(false)).toBe("비활성");
  });

  it("헬스: true 정상, false 오류, null·undefined 미확인", () => {
    expect(healthLabel(true)).toBe("정상");
    expect(healthLabel(false)).toBe("오류");
    expect(healthLabel(null)).toBe("미확인");
    expect(healthLabel(undefined)).toBe("미확인");
  });
});

describe("기존 사전 재사용·재수출 (AC-1.6)", () => {
  it("KIND_LABEL·RUN_STATUS_LABEL은 원래 모듈의 같은 객체다", () => {
    expect(KIND_LABEL).toBe(AGENT_KIND_LABEL);
    expect(RUN_STATUS_LABEL).toBe(AGENT_RUN_STATUS_LABEL);
  });

  it("실행 상태: 기존 라벨 + 미지 값 폴백", () => {
    expect(runStatusLabel("queued")).toBe("대기");
    expect(runStatusLabel("running")).toBe("실행 중");
    expect(runStatusLabel("succeeded")).toBe("완료");
    expect(runStatusLabel("failed")).toBe("실패");
    expect(runStatusLabel("cancelled")).toBe("취소됨");
    expect(runStatusLabel("awaiting_approval")).toBe("승인 대기");
    expect(runStatusLabel("paused")).toBe("paused");
  });

  it("에이전트 유형: 기존 라벨 + 미지 값 폴백", () => {
    expect(kindLabel("workflow")).toBe("워크플로우");
    expect(kindLabel("mcp")).toBe("MCP");
    expect(kindLabel("agentic")).toBe("agentic");
  });
});

describe("범위·능력 (AC-1.6)", () => {
  it("AGENT_SCOPE_LABEL: system → 시스템, user → 사용자, 그 밖은 원시값", () => {
    expect(AGENT_SCOPE_LABEL).toEqual({ system: "시스템", user: "사용자" });
    expect(agentScopeLabel("system")).toBe("시스템");
    expect(agentScopeLabel("user")).toBe("사용자");
    expect(agentScopeLabel("team:42")).toBe("team:42");
  });

  it("미지 범위 값에 콘솔 경고를 내지 않는다", () => {
    const warn = vi.spyOn(console, "warn");
    const error = vi.spyOn(console, "error");
    try {
      agentScopeLabel("team:42");
      auditActionLabel("unknown.code");
      expect(warn).not.toHaveBeenCalled();
      expect(error).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
      error.mockRestore();
    }
  });

  it("CAPABILITY_LABEL: 능력 5종 전부(MODALITIES 순서), 그 밖은 원시값", () => {
    expect(capabilityLabel("llm")).toBe("텍스트 생성(LLM)");
    expect(capabilityLabel("vlm")).toBe("이미지 이해(VLM)");
    expect(capabilityLabel("embedding")).toBe("임베딩");
    expect(capabilityLabel("tts")).toBe("tts");
    expect(capabilityLabel("reranker")).toBe("리랭커");
    expect(capabilityLabel("image")).toBe("이미지 생성");
    expect(capabilityLabel("video")).toBe("video");
    expect(Object.keys(CAPABILITY_LABEL)).toEqual([...MODALITIES]);
  });
});

describe("auditActionLabel (AC-1.6)", () => {
  it("알려진 코드는 한국어 라벨", () => {
    expect(auditActionLabel("llm.provider_create")).toBe("LLM 프로바이더 생성");
    expect(auditActionLabel("llm.provider_update")).toBe("LLM 프로바이더 수정");
    expect(auditActionLabel("llm.provider_delete")).toBe("LLM 프로바이더 삭제");
    expect(auditActionLabel("llm.model_create")).toBe("모델 생성");
    expect(auditActionLabel("llm.model_update")).toBe("모델 수정");
    expect(auditActionLabel("llm.model_delete")).toBe("모델 삭제");
    expect(auditActionLabel("rag.settings_update")).toBe("RAG 설정 변경");
    expect(auditActionLabel("rag.reindex")).toBe("재인덱싱 요청");
  });

  it("사전의 모든 키가 비어 있지 않은 라벨을 갖는다", () => {
    for (const [code, label] of Object.entries(AUDIT_ACTION_LABEL)) {
      expect(label.trim(), code).not.toBe("");
      expect(label, code).not.toBe(code);
    }
  });

  it("미지 코드는 원시 코드를 그대로 보인다", () => {
    // P5에서 실제 코드(workflow.publish 등)를 사전에 채웠으므로 어디에도 없는 코드로 미지 경로를 본다
    expect(auditActionLabel("future.unknown_action")).toBe("future.unknown_action");
    expect(auditActionLabel("llm.provider_create_v2")).toBe("llm.provider_create_v2");
  });

  it("코드가 없으면 「—」", () => {
    expect(auditActionLabel(null)).toBe("—");
    expect(auditActionLabel("")).toBe("—");
  });
});

describe("notebookAccessLabel — 남의 노트북을 볼 수 있는 까닭", () => {
  it("공유 기록 없이 공개된 노트북(읽기)은 「공개」, 그 밖은 「공유받음」", () => {
    expect(notebookAccessLabel({ visibility: "public", permission: "read" })).toBe("공개");
    expect(notebookAccessLabel({ visibility: "private", permission: "read" })).toBe("공유받음");
    expect(notebookAccessLabel({ visibility: "group", permission: "write" })).toBe("공유받음");
  });

  it("편집 권한은 공유로만 생기므로 공개 노트북이라도 write면 「공유받음」", () => {
    expect(notebookAccessLabel({ visibility: "public", permission: "write" })).toBe("공유받음");
  });
});

describe("공유 대상 표기·권한 선택지(core-v0.20.0 §5.8 ② — AC-3.1 대상 구별)", () => {
  const groups = new Map([
    ["a1111111-0000-4000-8000-000000000001", "연구팀"],
    ["b2222222-0000-4000-8000-000000000002", "가나 그룹"],
  ]);
  const row = (group_id: string) => ({ group_id });

  it("소유자 소속 그룹은 이름, 비소속 그룹은 「소유자가 속하지 않은 그룹 · id 앞 8자」(그룹 대상뿐 — 코어 v0.28.0 #278 3단계)", () => {
    expect(describeShareTarget(row("a1111111-0000-4000-8000-000000000001"), groups)).toEqual({
      kind: "group",
      label: "연구팀",
      editable: true,
    });
    expect(describeShareTarget(row("cccccccc-0000-4000-8000-000000000003"), groups)).toEqual({
      kind: "foreign-group",
      label: `${FOREIGN_GROUP_LABEL} · cccccccc`,
      editable: false,
    });
    expect(FOREIGN_GROUP_LABEL).toBe("소유자가 속하지 않은 그룹");
  });

  it("같은 종류의 행이 여럿이어도 id 앞 8자가 다르면 표기가 서로 다르다", () => {
    const labels = [row("cccccccc-3"), row("dddddddd-4")].map((r) => describeShareTarget(r, groups).label);
    expect(new Set(labels).size).toBe(2);
  });

  it("그룹 목록을 모르면(null — 조회 실패) 비소속으로 단정하지 않고 「그룹 · id 앞 8자」·group이지만, 소속이 확인되지 않았으므로 권한 변경 대상이 아니다(editable false — PR #154 P3-04)", () => {
    expect(describeShareTarget(row("cccccccc-0000"), null)).toEqual({
      kind: "group",
      label: "그룹 · cccccccc",
      editable: false,
    });
    // 같은 그룹이 소속 목록에 있으면(조회 성공) 그때만 편집 가능
    expect(describeShareTarget(row("cccccccc-0000"), new Map([["cccccccc-0000", "다라 그룹"]]))).toEqual({
      kind: "group",
      label: "다라 그룹",
      editable: true,
    });
    // 조회는 성공했고 소속이 아니면 비소속 — 편집 불가
    expect(describeShareTarget(row("cccccccc-0000"), new Map())).toEqual({
      kind: "foreign-group",
      label: `${FOREIGN_GROUP_LABEL} · cccccccc`,
      editable: false,
    });
  });

  it("권한 선택지는 read·write 순서이고 이름은 「읽기 전용」·「편집 가능」(괄호 영문 없음 — UI 계약)", () => {
    expect(SHARE_PERMISSION_OPTIONS).toEqual([
      { value: "read", label: NOTEBOOK_PERMISSION_LABEL.read },
      { value: "write", label: NOTEBOOK_PERMISSION_LABEL.write },
    ]);
    expect(SHARE_PERMISSION_OPTIONS.map((o) => o.label)).toEqual(["읽기 전용", "편집 가능"]);
  });
});
