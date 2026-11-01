import { describe, expect, it } from "vitest";
import autonomousDone from "@/lib/__fixtures__/sse-done/autonomous.json";
import legacyDone from "@/lib/__fixtures__/sse-done/legacy.json";
import staticDone from "@/lib/__fixtures__/sse-done/static.json";
import type { ChatStreamEvent, Citation } from "@/lib/chat-types";
import {
  applyChatEvent,
  drainSseBuffer,
  INITIAL_STREAM,
  selectStreamingEvidence,
} from "@/lib/sse";

/**
 * [BL-22 / PR-16 · RR-02] 세 fixture는 백엔드 테스트(`apps/api/tests/test_chat_api.py`)가
 * **실제 `done` 페이로드**로 대조하는 canonical 계약이다. 여기서 같은 파일을 리듀서
 * 입력으로 소비해 라이브 경로 계약이 양쪽에서 갈라지지 않게 한다.
 * 재생성: `PYTEST_UPDATE_SSE_FIXTURES=1 uv run pytest tests/test_chat_api.py -k sse_done_fixture`
 */
const doneEvent = (fixture: unknown): ChatStreamEvent => fixture as ChatStreamEvent;

const citation = (n: number): Citation => ({
  n,
  source_id: `src-${n}`,
  chunk_id: `chunk-${n}`,
  source_title: `소스 ${n}`,
  quote: `발췌 ${n}`,
  verified: true,
});

describe("drainSseBuffer", () => {
  it("완성된 프레임을 이벤트로 파싱한다", () => {
    const buf = 'data: {"type":"delta","text":"안녕"}\n\n';
    const { events, rest } = drainSseBuffer(buf);
    expect(events).toEqual([{ type: "delta", text: "안녕" }]);
    expect(rest).toBe("");
  });

  it("미완성 프레임은 rest로 보존한다", () => {
    const buf = 'data: {"type":"delta","text":"a"}\n\ndata: {"type":"del';
    const { events, rest } = drainSseBuffer(buf);
    expect(events).toHaveLength(1);
    expect(rest).toBe('data: {"type":"del');
  });

  it("한 버퍼의 여러 프레임을 순서대로 파싱한다", () => {
    const buf =
      'data: {"type":"delta","text":"a"}\n\ndata: {"type":"delta","text":"b"}\n\n';
    const { events } = drainSseBuffer(buf);
    expect(events.map((e) => (e.type === "delta" ? e.text : ""))).toEqual(["a", "b"]);
  });

  it("data: 라인이 없는 프레임은 무시한다", () => {
    const { events } = drainSseBuffer(": keepalive\n\n");
    expect(events).toEqual([]);
  });

  it("잘못된 JSON 프레임은 건너뛰고 다음 프레임을 처리한다", () => {
    const buf = 'data: {broken\n\ndata: {"type":"delta","text":"ok"}\n\n';
    const { events } = drainSseBuffer(buf);
    expect(events).toEqual([{ type: "delta", text: "ok" }]);
  });
});

describe("applyChatEvent", () => {
  it("retrieval — 인용 목록을 채운다", () => {
    const s = applyChatEvent(INITIAL_STREAM, {
      type: "retrieval",
      citations: [citation(1)],
    });
    expect(s.citations).toHaveLength(1);
    expect(s.content).toBe("");
  });

  it("delta — 내용을 누적한다", () => {
    let s = applyChatEvent(INITIAL_STREAM, { type: "delta", text: "안녕" });
    s = applyChatEvent(s, { type: "delta", text: "하세요" });
    expect(s.content).toBe("안녕하세요");
  });

  it("done — 검증된 최종 인용으로 교체한다", () => {
    const before = { ...INITIAL_STREAM, citations: [citation(1), citation(2)] };
    const final = { ...citation(1), verified: false };
    const s = applyChatEvent(before, {
      type: "done",
      message_id: "m1",
      citations: [final],
      token_usage: {},
      model: "mock",
    });
    expect(s.citations).toEqual([final]);
  });

  it("error — 에러 메시지를 기록하고 기존 내용을 보존한다", () => {
    const before = { ...INITIAL_STREAM, content: "부분 응답" };
    const s = applyChatEvent(before, { type: "error", detail: "요청이 너무 많습니다" });
    expect(s.error).toBe("요청이 너무 많습니다");
    expect(s.content).toBe("부분 응답");
  });

  it("retrieval — insufficient_context 플래그를 반영한다 (18b)", () => {
    const s = applyChatEvent(INITIAL_STREAM, {
      type: "retrieval",
      citations: [],
      insufficient_context: true,
    });
    expect(s.insufficientContext).toBe(true);
  });

  it("retrieval — insufficient_context 미포함 시 false로 초기화한다", () => {
    const before = { ...INITIAL_STREAM, insufficientContext: true };
    const s = applyChatEvent(before, { type: "retrieval", citations: [] });
    expect(s.insufficientContext).toBe(false);
  });

  it("done — insufficient_context 플래그를 반영한다 (18b)", () => {
    const s = applyChatEvent(INITIAL_STREAM, {
      type: "done",
      message_id: "m1",
      citations: [],
      token_usage: {},
      model: "mock",
      insufficient_context: true,
    });
    expect(s.insufficientContext).toBe(true);
  });

  it("done — provenance를 스트림 상태에 보존한다 (BL-22)", () => {
    const s = applyChatEvent(INITIAL_STREAM, {
      type: "done",
      message_id: "m1",
      citations: [],
      token_usage: {},
      model: "mock",
      provenance: {
        v: 1,
        mode: "autonomous",
        recorded_at: "2026-01-01T00:00:00Z",
        run_id: null,
        artifact_name: null,
        items: [{ kind: "mcp", status: "succeeded", server: "archhub", tool: "find_region" }],
      },
    });
    expect(s.provenance?.items).toHaveLength(1);
    expect(s.provenance?.items?.[0].kind).toBe("mcp");
    expect(s.legacyEvidence).toEqual([]);
  });
});

describe("applyChatEvent — 스킬 설치 제안 (#259)", () => {
  it("skill_install_offer — 카드 데이터를 보존하고 다른 이벤트는 건드리지 않는다", () => {
    expect(INITIAL_STREAM.installOffer).toBeNull();
    const offer = { owner: "o", repo: "r", sha: "a".repeat(40), candidates: [] } as never;
    let state = applyChatEvent(INITIAL_STREAM, { type: "skill_install_offer", offer });
    expect(state.installOffer).toBe(offer);
    state = applyChatEvent(state, { type: "delta", text: "후보 0개" });
    expect(state).toMatchObject({ installOffer: offer, content: "후보 0개" });
  });

  it("모르는 이벤트는 무시한다 — 옛 web이 새 이벤트를 받아도 깨지지 않는 것과 같은 규칙", () => {
    const state = applyChatEvent(INITIAL_STREAM, { type: "future_event" } as never);
    expect(state).toBe(INITIAL_STREAM);
  });
});

describe("applyChatEvent — done fixture 계약 3종 (BL-22, AC-15)", () => {
  it("autonomous fixture — provenance.items를 배지 입력으로 채택한다", () => {
    const s = applyChatEvent(INITIAL_STREAM, doneEvent(autonomousDone));
    expect(s.provenance?.mode).toBe("autonomous");
    expect(s.provenance?.items?.map((i) => i.tool)).toEqual(["rag_search"]);
    // fallback 경로는 쓰이지 않는다 — provenance가 있으면 그것이 진실 공급원이다.
    expect(selectStreamingEvidence(s)).toBe(s.provenance?.items);
    expect(s.legacyEvidence).toEqual([]);
  });

  it("static fixture — kind=source items로 라이브 소스 배지를 그린다 (HCI-P2-6)", () => {
    const s = applyChatEvent(INITIAL_STREAM, doneEvent(staticDone));
    expect(s.provenance?.mode).toBe("static");
    expect(s.provenance?.items?.map((i) => i.kind)).toEqual(["source"]);
    expect(s.citations).toHaveLength(1);
  });

  it("legacy fixture — provenance 부재 시 evidence로 1 릴리스 fallback한다 (HCI-P2-5)", () => {
    const s = applyChatEvent(INITIAL_STREAM, doneEvent(legacyDone));
    expect(s.provenance).toBeNull();
    expect(s.legacyEvidence.map((i) => i.kind)).toEqual(["mcp"]);
    // chat-panel.tsx가 쓰는 바로 그 선택 함수(P4-02).
    expect(selectStreamingEvidence(s)).toBe(s.legacyEvidence);
    expect(selectStreamingEvidence(s).map((i) => i.kind)).toEqual(["mcp"]);
  });
});

describe("applyChatEvent — provenance 키 부재 vs null 구분 (P4-03)", () => {
  /** 서버 빌더 실패: 키는 **있고** 값이 null이며 deprecated evidence는 여전히 온다. */
  const builderFailed = (): ChatStreamEvent => ({
    type: "done",
    message_id: "m1",
    citations: [],
    token_usage: {},
    model: "mock",
    provenance: null,
    evidence: [{ kind: "mcp", status: "succeeded", server: "archhub", tool: "find_region" }],
  });

  it("키가 있고 값이 null이면 fallback하지 않는다 — 새로고침 후와 같이 배지가 없다", () => {
    const s = applyChatEvent(INITIAL_STREAM, builderFailed());
    expect(s.provenance).toBeNull();
    // 저장된 ChatMessageOut.provenance도 null이므로 라이브에도 배지가 없어야 한다.
    expect(s.legacyEvidence).toEqual([]);
    expect(selectStreamingEvidence(s)).toEqual([]);
  });

  it("키가 아예 없으면(구 API) evidence로 fallback한다", () => {
    // 구 API 응답은 `provenance` 키 자체가 없다 — 키를 지워 그 형태를 재현한다.
    // (구조 분해로 키를 버리면 미사용 바인딩이 남아 no-unused-vars가 경고한다:
    //  이 설정의 규칙은 옵션이 없어 `_` 접두 예외(varsIgnorePattern)가 적용되지 않는다.)
    const keyAbsent = { ...(builderFailed() as Record<string, unknown>) };
    delete keyAbsent.provenance;
    const s = applyChatEvent(INITIAL_STREAM, keyAbsent as unknown as ChatStreamEvent);
    expect(s.provenance).toBeNull();
    expect(selectStreamingEvidence(s).map((i) => i.kind)).toEqual(["mcp"]);
  });
});
