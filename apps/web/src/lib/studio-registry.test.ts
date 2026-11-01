/** 스튜디오 카드 레지스트리(AC-1.9·1.10 · #180) — 바인딩(스킬·시스템 에이전트), 에이전트 id 기준 결과 판정. */
import { describe, expect, it } from "vitest";
import type { Agent, AgentRun } from "@/lib/agent-types";
import type { Skill } from "@/lib/skill-types";
import {
  findStudioAgent,
  SLIDE_DECK_CARD_ID,
  SLIDE_OUTLINE_CARD_ID,
  STUDIO_CARDS,
  studioCardById,
  VISIBLE_STUDIO_CARDS,
  studioArtifact,
  studioCardForRun,
  studioCardsByAgentId,
} from "@/lib/studio-registry";

function skill(overrides: Partial<Skill>): Skill {
  return {
    id: "sk1",
    name: "마인드맵",
    description: "소스 마인드맵",
    slug: "mindmap",
    visibility: "system",
    enabled: true,
    can_run: true,
    wrapper_agent_id: "wrap-1",
    params_schema: { type: "object", properties: { topic: { type: "string" } } },
    input: { mode: "rag", scope: "selected", max_sources: 12 },
    ...overrides,
  } as Skill;
}

function agent(overrides: Partial<Agent>): Agent {
  return {
    id: "ag-quiz",
    name: "퀴즈 생성",
    description: "퀴즈",
    icon: "❓",
    kind: "workflow",
    scope: "system",
    enabled: true,
    version: 1,
    params_schema: { type: "object" },
    ...overrides,
  } as Agent;
}

function run(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: "r1",
    agent_id: "wrap-1",
    status: "succeeded",
    artifact_keys: [{ key: "r1/mindmap.json", name: "mindmap.json", content_type: "application/json" }],
    ...overrides,
  } as AgentRun;
}

const card = (id: string) => STUDIO_CARDS.find((c) => c.id === id)!;

/** 시드와 같은 모양의 목록 — 10개 카드(숨김 2개 포함)가 모두 풀린다 */
const FULL = {
  skills: [
    skill({}),
    // 은퇴한 옛 스킬 패키지(#282)가 남아 있어도 카드에 묶이지 않는다
    skill({ id: "sk-r", slug: "report-writer", name: "보고서 생성", wrapper_agent_id: "w-report" }),
  ],
  agents: [
    agent({ id: "ag-report", name: "보고서 생성" }),
    agent({ id: "ag-brief", name: "브리핑 문서" }),
    agent({ id: "ag-study", name: "스터디 가이드" }),
    agent({ id: "ag-slide", name: "슬라이드 개요" }),
    agent({}),
    agent({ id: "ag-flash", name: "플래시카드 생성" }),
    agent({ id: "ag-info", name: "인포그래픽 데이터" }),
    agent({ id: "ag-deck", name: "슬라이드 자료" }),
    agent({ id: "ag-outline", name: "슬라이드 자료 개요" }),
    agent({ id: "ag-image", name: "슬라이드 삽화" }),
    agent({ id: "ag-write", name: "슬라이드 장 작성" }),
  ],
};

describe("카드 목록", () => {
  it("마인드맵 + 1차 5종 + 슬라이드 자료·개요·삽화·장 작성 = 10개, 대표 산출물이 시드 산출물과 같다", () => {
    expect(STUDIO_CARDS.map((c) => [c.id, c.artifactName])).toEqual([
      ["mindmap", "mindmap.json"],
      ["report", "report.md"],
      ["briefing", "briefing.md"],
      ["study-guide", "study-guide.md"],
      ["quiz", "quiz.json"],
      ["flashcards", "flashcards.json"],
      ["slide-deck", "slide-deck.json"],
      ["slide-deck-outline", "slide-outline.json"],
      ["slide-deck-image", "slide-image-1.png"],
      ["slide-deck-write", ""], // loop 본문 — 산출물을 저장하지 않는다
    ]);
    expect(new Set(STUDIO_CARDS.map((c) => c.id)).size).toBe(STUDIO_CARDS.length);
  });

  it("개요·삽화·장 작성 카드는 숨김 — 띠에는 7개, id 상수는 카드와 같다", () => {
    expect(VISIBLE_STUDIO_CARDS.map((c) => c.id)).not.toContain(SLIDE_OUTLINE_CARD_ID);
    expect(VISIBLE_STUDIO_CARDS.map((c) => c.id)).not.toContain("slide-deck-image");
    expect(VISIBLE_STUDIO_CARDS.map((c) => c.id)).not.toContain("slide-deck-write");
    expect(studioCardById("slide-deck-image").hidden).toBe(true);
    expect(studioCardById("slide-deck-write").hidden).toBe(true);
    expect(VISIBLE_STUDIO_CARDS).toHaveLength(7);
    expect(studioCardById(SLIDE_DECK_CARD_ID).title).toBe("json-title");
    expect(studioCardById(SLIDE_OUTLINE_CARD_ID).hidden).toBe(true);
    expect(() => studioCardById("nope")).toThrow();
  });

  it("설명은 짧은 사용자 문구다 — 구현 용어(JSON·워크플로우·스킬)를 쓰지 않는다", () => {
    for (const c of STUDIO_CARDS) {
      expect(c.description).not.toMatch(/JSON|워크플로우|스킬|에이전트/);
      expect(c.description.length).toBeLessThanOrEqual(24);
    }
  });
});

describe("findStudioAgent — 스킬 바인딩", () => {
  const mm = card("mindmap");
  it("slug ∧ visibility=system ∧ 실행 가능한 스킬의 wrapper에 묶인다", () => {
    const found = findStudioAgent(mm, {
      skills: [skill({ id: "x", visibility: "private", wrapper_agent_id: "w-x" }), skill({})],
    });
    expect(found).toMatchObject({ id: "wrap-1", kind: "skill", scope: "system", name: "마인드맵" });
    expect(found?.input).toEqual({ mode: "rag", scope: "selected", max_sources: 12 });
  });

  it.each([
    ["다른 slug", { slug: "mindmap-2" }],
    ["org 공개", { visibility: "org" }],
    ["실행 불가", { can_run: false }],
    ["wrapper 없음", { wrapper_agent_id: null }],
    ["꺼짐", { enabled: false }],
  ] as [string, Partial<Skill>][])("%s → null", (_l, o) => {
    expect(findStudioAgent(mm, { skills: [skill(o)] })).toBeNull();
  });

  it("목록이 없으면 null", () => {
    expect(findStudioAgent(mm, {})).toBeNull();
  });

  it("보고서·브리핑은 시스템 워크플로우에 묶이고 은퇴한 스킬 wrapper에는 묶이지 않는다(#282)", () => {
    expect(findStudioAgent(card("report"), FULL)?.id).toBe("ag-report");
    expect(findStudioAgent(card("briefing"), FULL)?.id).toBe("ag-brief");
    expect(findStudioAgent(card("report"), { skills: FULL.skills })).toBeNull();
  });
});

describe("findStudioAgent — 시스템 에이전트 바인딩", () => {
  it("이름 ∧ kind ∧ scope=system ∧ 켜짐인 에이전트", () => {
    expect(findStudioAgent(card("quiz"), FULL)?.id).toBe("ag-quiz");
    expect(findStudioAgent(card("flashcards"), FULL)?.id).toBe("ag-flash");
    expect(findStudioAgent(card("study-guide"), FULL)?.id).toBe("ag-study");
  });

  it.each([
    ["사용자 scope 동명 에이전트", { scope: "user" }],
    ["kind가 다름", { kind: "skill" }],
    ["꺼짐", { enabled: false }],
    ["이름이 다름", { name: "퀴즈 생성 (복사본)" }],
  ] as [string, Partial<Agent>][])("%s → null", (_l, o) => {
    expect(findStudioAgent(card("quiz"), { agents: [agent(o)] })).toBeNull();
  });
});

describe("studioCardsByAgentId", () => {
  it("풀린 카드만 에이전트 id로 담는다 — 숨김 카드 포함, 슬라이드 개요 스킬·인포그래픽은 없다", () => {
    const map = studioCardsByAgentId(FULL);
    expect(Object.fromEntries([...map].map(([id, c]) => [id, c.id]))).toEqual({
      "wrap-1": "mindmap",
      "ag-report": "report",
      "ag-brief": "briefing",
      "ag-study": "study-guide",
      "ag-quiz": "quiz",
      "ag-flash": "flashcards",
      "ag-deck": "slide-deck",
      "ag-outline": "slide-deck-outline",
      "ag-image": "slide-deck-image",
      "ag-write": "slide-deck-write",
    });
    expect(studioCardsByAgentId({}).size).toBe(0);
  });
});

describe("studioCardForRun — 에이전트 id 기준(#180 결정 1)", () => {
  const map = studioCardsByAgentId(FULL);

  it("카드 에이전트의 성공 run ∧ 대표 산출물 있음 → 카드", () => {
    expect(studioCardForRun(run({}), map)?.id).toBe("mindmap");
    const quiz = run({
      agent_id: "ag-quiz",
      artifact_keys: [
        { key: "r1/notes.md", name: "notes.md", content_type: "text/markdown" },
        { key: "r1/quiz.json", name: "quiz.json", content_type: "application/json" },
      ],
    });
    expect(studioCardForRun(quiz, map)?.id).toBe("quiz");
    expect(studioArtifact(quiz, card("quiz"))?.key).toBe("r1/quiz.json");
  });

  it("실패·실행 중·대표 산출물 없음은 일반 행", () => {
    expect(studioCardForRun(run({ status: "failed" }), map)).toBeNull();
    expect(studioCardForRun(run({ status: "running" }), map)).toBeNull();
    expect(studioCardForRun(run({ artifact_keys: [] }), map)).toBeNull();
    // 대표 산출물(quiz.json) 없이 다른 산출물만 남은 run
    const notesOnly = run({
      agent_id: "ag-quiz",
      artifact_keys: [{ key: "r1/notes.md", name: "notes.md", content_type: "text/markdown" }],
    });
    expect(studioCardForRun(notesOnly, map)).toBeNull();
  });

  it("사용자 스킬이 같은 이름의 산출물(quiz.json·mindmap.json)을 내도 스튜디오 항목이 아니다", () => {
    const quizLike = run({
      agent_id: "user-skill-wrapper",
      artifact_keys: [{ key: "r1/quiz.json", name: "quiz.json", content_type: "application/json" }],
    });
    expect(studioCardForRun(quizLike, map)).toBeNull();
    expect(studioCardForRun(run({ agent_id: "user-skill-wrapper" }), map)).toBeNull();
  });

  it("은퇴한 워크플로우 「마인드맵」의 옛 run(다른 에이전트 id)은 일반 행", () => {
    expect(studioCardForRun(run({ agent_id: "ag-retired-mindmap-workflow" }), map)).toBeNull();
  });
});
