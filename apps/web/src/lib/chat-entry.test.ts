import { describe, expect, it } from "vitest";
import {
  ambiguousCandidates,
  entryRunPollInterval,
  ENTRY_RUN_STATUS_LABEL,
  filterEntries,
  isActiveRunStatus,
  paletteQuery,
  parseSlashInput,
  stripFirstToken,
} from "@/lib/chat-entry";
import type { ChatEntry } from "@/lib/chat-types";

const entry = (over: Partial<ChatEntry>): ChatEntry => ({
  agent_id: "a",
  kind: "skill",
  slash_name: "brief",
  source: "own",
  display_name: "브리핑",
  description: "짧게 요약한다",
  ...over,
});

describe("paletteQuery — 팔레트 열림 조건(#66 §3 웹)", () => {
  it("첫 글자가 `/`이고 커서가 첫 토큰 안이면 필터어를 돌려준다", () => {
    expect(paletteQuery("/", 1)).toBe("");
    expect(paletteQuery("/bri", 4)).toBe("bri");
    expect(paletteQuery("/bri", 2)).toBe("bri"); // 커서가 토큰 중간이어도 토큰 전체로 거른다
    expect(paletteQuery("/brief 질문", 6)).toBe("brief"); // 토큰 끝(공백 직전)
  });

  it("첫 글자가 `/`가 아니거나 커서가 첫 토큰 밖이면 열지 않는다", () => {
    expect(paletteQuery("a/b", 3)).toBeNull();
    expect(paletteQuery(" /b", 3)).toBeNull();
    expect(paletteQuery("$brief", 6)).toBeNull(); // `$` 접두는 받지 않는다(결정 1)
    expect(paletteQuery("/brief 질문", 7)).toBeNull(); // 공백 뒤 — 질문을 쓰는 중
    expect(paletteQuery("/brief", 0)).toBeNull(); // `/` 앞
    expect(paletteQuery("", 0)).toBeNull();
  });
});

describe("filterEntries", () => {
  const list = [
    entry({ agent_id: "1", slash_name: "brief", display_name: "브리핑", description: "요약" }),
    entry({ agent_id: "2", slash_name: null, display_name: "주간 리포트", description: "매주 정리", kind: "workflow" }),
    entry({ agent_id: "3", slash_name: "quiz", display_name: "퀴즈", description: "문제를 만든다" }),
  ];

  it("slash_name·표시 이름·설명 어디에 들어 있어도 맞고, 대소문자를 가리지 않으며 순서는 그대로다", () => {
    expect(filterEntries(list, "BRI").map((e) => e.agent_id)).toEqual(["1"]);
    expect(filterEntries(list, "리포트").map((e) => e.agent_id)).toEqual(["2"]);
    expect(filterEntries(list, "만든다").map((e) => e.agent_id)).toEqual(["3"]);
    expect(filterEntries(list, "").map((e) => e.agent_id)).toEqual(["1", "2", "3"]);
    expect(filterEntries(list, "없음")).toEqual([]);
  });
});

describe("parseSlashInput · stripFirstToken", () => {
  it("칩 없는 `/<이름> 질문`을 이름과 질문으로 나눈다", () => {
    expect(parseSlashInput("/brief 이번 주 요약")).toEqual({ name: "brief", question: "이번 주 요약" });
    expect(parseSlashInput("/brief")).toEqual({ name: "brief", question: "" });
    expect(parseSlashInput("/brief\n여러 줄\n질문")).toEqual({ name: "brief", question: "여러 줄\n질문" });
    expect(parseSlashInput("/ 질문")).toBeNull(); // 이름이 비었다
    expect(parseSlashInput("$brief 질문")).toBeNull();
    expect(parseSlashInput("brief 질문")).toBeNull();
  });

  it("항목을 고르면 첫 토큰을 지우고 질문만 남긴다", () => {
    expect(stripFirstToken("/bri 이번 주")).toBe("이번 주");
    expect(stripFirstToken("/bri")).toBe("");
  });
});

describe("ambiguousCandidates", () => {
  it("409 `chat_entry_ambiguous` 본문의 후보만 꺼내고, 모양이 다르면 빈 목록이다", () => {
    const candidates = [entry({ agent_id: "x" })];
    expect(ambiguousCandidates({ detail: { code: "chat_entry_ambiguous", candidates } })).toEqual(candidates);
    expect(ambiguousCandidates({ detail: "이 대화에서 이미 실행 중인 에이전트가 있습니다" })).toEqual([]);
    expect(ambiguousCandidates({ detail: { code: "other", candidates } })).toEqual([]);
    expect(ambiguousCandidates(null)).toEqual([]);
  });
});

describe("entryRunPollInterval — run 카드 폴링(리뷰 Medium)", () => {
  it("읽은 상태가 있으면 그 상태로 정한다", () => {
    expect(entryRunPollInterval("running", "running")).toBe(2000);
    expect(entryRunPollInterval("awaiting_approval", "running")).toBe(2000);
    expect(entryRunPollInterval("succeeded", "running")).toBe(false);
  });

  it("못 읽었으면 일시 오류는 알려진 상태가 끝나지 않은 동안 계속 보고, 4xx는 멈춘다", () => {
    expect(entryRunPollInterval(undefined, "running", 500)).toBe(5000);
    expect(entryRunPollInterval(undefined, "queued")).toBe(5000); // 네트워크 오류(상태 코드 없음)·첫 응답 전
    expect(entryRunPollInterval(undefined, "running", 404)).toBe(false);
    expect(entryRunPollInterval(undefined, "succeeded", 500)).toBe(false);
    expect(entryRunPollInterval(undefined, undefined, 500)).toBe(false);
  });
});

describe("run 상태", () => {
  it("끝나지 않은 상태는 대기·실행 중·승인 대기이고, 취소는 「중지」로 보인다", () => {
    expect(["queued", "running", "awaiting_approval"].every(isActiveRunStatus)).toBe(true);
    expect(["succeeded", "failed", "cancelled"].some(isActiveRunStatus)).toBe(false);
    expect(ENTRY_RUN_STATUS_LABEL.cancelled).toBe("중지");
  });
});
