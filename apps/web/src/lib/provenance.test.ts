import { describe, expect, it } from "vitest";
import type { Note } from "@/lib/note-types";
import {
  filterDisplayableItems,
  getNoteProvenanceView,
  NOTE_PROVENANCE_COPY,
  toProvenanceBadge,
  toProvenanceBadges,
  type ProvenanceItem,
} from "@/lib/provenance";

function note(overrides: Partial<Note>): Note {
  return {
    id: "n1",
    notebook_id: "nb1",
    author_id: "u1",
    title: "제목",
    content: "본문",
    origin: "chat",
    origin_ref: "m1",
    citations: [],
    provenance: null,
    content_intact: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

describe("toProvenanceBadge", () => {
  it("skill — 이름·버전을 라벨에 싣는다", () => {
    const item: ProvenanceItem = {
      kind: "skill",
      status: "succeeded",
      name: "건축물 용도변경 검토 보고서",
      version: 3,
    };
    const badge = toProvenanceBadge(item, 0);
    expect(badge.label).toBe("스킬 건축물 용도변경 검토 보고서 v3");
    expect(badge.failed).toBe(false);
  });

  it("mcp — 서버/도구 이름을 라벨에 싣는다", () => {
    const item: ProvenanceItem = {
      kind: "mcp",
      status: "succeeded",
      server: "archhub",
      tool: "building_profile",
    };
    expect(toProvenanceBadge(item, 0).label).toBe("MCP archhub/building_profile");
  });

  it("실패 상태 — 라벨에 '실패' 접미사가 붙고 failed=true", () => {
    const item: ProvenanceItem = {
      kind: "mcp",
      status: "failed",
      server: "archhub",
      tool: "building_profile",
    };
    const badge = toProvenanceBadge(item, 0);
    expect(badge.label).toContain("실패");
    expect(badge.failed).toBe(true);
  });

  it("unknown 상태 — 라벨에 '상태 미상' 접미사가 붙는다", () => {
    const item: ProvenanceItem = { kind: "tool", status: "unknown", tool: "rag_search" };
    expect(toProvenanceBadge(item, 0).label).toContain("상태 미상");
  });

  it("source — 제목 우선, 없으면 source_id로 대체한다", () => {
    const withTitle: ProvenanceItem = {
      kind: "source",
      status: "succeeded",
      source_id: "s1",
      title: "건축법 시행령",
    };
    expect(toProvenanceBadge(withTitle, 0).label).toBe("소스 건축법 시행령");
    const withoutTitle: ProvenanceItem = { kind: "source", status: "succeeded", source_id: "s1" };
    expect(toProvenanceBadge(withoutTitle, 0).label).toBe("소스 s1");
  });
});

describe("filterDisplayableItems / toProvenanceBadges — BL-17 이중 기록 접기", () => {
  it("run_skill의 kind=tool 중복 항목을 접고 kind=skill만 남긴다", () => {
    const items: ProvenanceItem[] = [
      { kind: "skill", status: "succeeded", name: "용도변경 검토", version: 1 },
      { kind: "tool", status: "succeeded", tool: "run_skill" },
    ];
    expect(filterDisplayableItems(items)).toHaveLength(1);
    expect(toProvenanceBadges(items)).toHaveLength(1);
    expect(toProvenanceBadges(items)[0].label).toContain("스킬");
  });

  it("성공한 mcp 호출의 kind=tool(mcp_ 접두사) 중복 항목을 접는다", () => {
    const items: ProvenanceItem[] = [
      { kind: "mcp", status: "succeeded", server: "archhub", tool: "building_profile" },
      { kind: "tool", status: "succeeded", tool: "mcp_archhub01_building_profile" },
    ];
    expect(filterDisplayableItems(items)).toHaveLength(1);
    expect(filterDisplayableItems(items)[0].kind).toBe("mcp");
  });

  it("rag_search 같은 일반 도구 항목은 접지 않는다", () => {
    const items: ProvenanceItem[] = [{ kind: "tool", status: "succeeded", tool: "rag_search" }];
    expect(filterDisplayableItems(items)).toHaveLength(1);
  });

  // phase-3-review.md P3-01 — 짝(kind=skill/mcp)이 없거나 succeeded가 아니면 유일한
  // 기록이므로 접으면 안 된다. 리뷰어가 재현한 3건을 회귀 테스트로 고정한다.
  it("P3-01 — 실패한 run_skill이 유일한 항목일 때 화면에서 사라지지 않는다", () => {
    const items: ProvenanceItem[] = [{ kind: "tool", status: "failed", tool: "run_skill" }];
    expect(filterDisplayableItems(items)).toHaveLength(1);
  });

  it("P3-01 — 차단된(blocked) MCP 호출이 유일한 항목일 때 화면에서 사라지지 않는다", () => {
    const items: ProvenanceItem[] = [
      { kind: "tool", status: "blocked", tool: "mcp_archhub1_building_profile" },
    ];
    expect(filterDisplayableItems(items)).toHaveLength(1);
  });

  it("P3-01 — origin=agent 노트에서 짝이 되는 kind=skill 항목이 없으면 run_skill 기록을 보존한다", () => {
    const items: ProvenanceItem[] = [
      { kind: "agent", status: "succeeded", name: "보고서 작성기" },
      { kind: "tool", status: "succeeded", tool: "run_skill" },
    ];
    const badges = toProvenanceBadges(items);
    expect(badges).toHaveLength(2);
    expect(badges.some((b) => b.label.includes("run_skill"))).toBe(true);
  });
});

describe("getNoteProvenanceView — AC-12 5상태", () => {
  it("(i) provenance=null → none", () => {
    const view = getNoteProvenanceView(note({ provenance: null, content_intact: null }));
    expect(view.state).toBe("none");
    expect(view.edited).toBe(false);
  });

  it("(ii) items가 있으면 모드 라벨 + 배지 목록", () => {
    const view = getNoteProvenanceView(
      note({
        provenance: {
          v: 1,
          mode: "autonomous",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [{ kind: "skill", status: "succeeded", name: "스킬A", version: 1 }],
        },
        content_intact: true,
      }),
    );
    expect(view.state).toBe("items");
    expect(view.modeLabel).toBe("자율 채팅");
    expect(view.badges).toHaveLength(1);
  });

  it("(iii) items=[] → empty", () => {
    const view = getNoteProvenanceView(
      note({
        provenance: {
          v: 1,
          mode: "static",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: true,
      }),
    );
    expect(view.state).toBe("empty");
  });

  it("(iv) content_intact=false → edited=true (다른 상태와 독립적으로 결합)", () => {
    const view = getNoteProvenanceView(
      note({
        provenance: {
          v: 1,
          mode: "static",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: false,
      }),
    );
    expect(view.edited).toBe(true);
    expect(view.state).toBe("empty");
  });

  it("(v) mode=unrecorded → unrecorded, 고정 문구", () => {
    const view = getNoteProvenanceView(
      note({
        provenance: {
          v: 1,
          mode: "unrecorded",
          recorded_at: "2026-09-03T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: true,
      }),
    );
    expect(view.state).toBe("unrecorded");
    expect(view.modeLabel).toBe("생성 근거 미기록");
    expect(NOTE_PROVENANCE_COPY.unrecorded).toContain("2026-09 기능 도입 이전");
  });
});
