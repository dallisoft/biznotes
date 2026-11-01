/**
 * 겹친 전송의 대기 user 메시지(mindmap-agent-skill AC-2.12 (d)·(d1)·(d2), R2-01).
 *
 * 화면은 스트리밍 중 새 전송을 막으므로 겹침은 훅 경로로 만든다 — 실제 useSendMessage·useMessages·api-client,
 * 대역은 fetch(지연 SSE·저장 메시지 GET)뿐이다. 화면에 보일 행은 ChatPanel과 같은 `visiblePending`으로 계산한다.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { useQueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Providers } from "@/components/providers";
import { useMessages, useSendMessage } from "@/hooks/use-chat";
import { visiblePending } from "@/lib/pending-messages";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/notebooks/nb1",
}));

function controlledSse(signal?: AbortSignal | null) {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  signal?.addEventListener("abort", () => {
    try {
      ctrl.error(new DOMException("aborted", "AbortError"));
    } catch {
      // 이미 닫힘
    }
  });
  return {
    response: new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    push: (e: object) => ctrl.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`)),
    close: () => ctrl.close(),
  };
}

let stored: unknown[] = [];
const streams: ReturnType<typeof controlledSse>[] = [];

beforeEach(() => {
  stored = [];
  streams.length = 0;
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/chat/sessions/s1/messages") && init?.method === "POST") {
        const s = controlledSse(init.signal);
        streams.push(s);
        return s.response;
      }
      return new Response(JSON.stringify(String(url).includes("/messages") ? stored : []), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const msg = (id: string, role: "user" | "assistant", content: string) => ({
  id,
  session_id: "s1",
  role,
  content,
  citations: [],
  model_id: null,
  token_usage: {},
  created_at: "2026-10-16T00:00:00Z",
});

function setup() {
  return renderHook(
    () => {
      const chat = useSendMessage("s1", "nb1");
      const messages = useMessages("s1");
      const client = useQueryClient();
      const saved = new Set((messages.data ?? []).map((m) => m.id));
      return { ...chat, messages, client, visible: visiblePending(chat.pending, "s1", saved) };
    },
    { wrapper: ({ children }) => <Providers>{children}</Providers> },
  );
}

const SAME = "같은 질문";
const done = (id: string) => ({ type: "done", message_id: id, citations: [], token_usage: {}, model: "m", provenance: null });

describe("useSendMessage — 겹친 전송(AC-2.12 (d))", () => {
  it("(d) B가 A를 abort한 뒤 A의 늦은 종료가 B의 상태·스트리밍 표시를 덮지 않는다", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.messages.isSuccess).toBe(true));
    let a!: Promise<unknown>;
    act(() => {
      a = result.current.send("A", null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => streams[0].push({ type: "delta", text: "A 답" }));
    let b!: Promise<unknown>;
    act(() => {
      b = result.current.send("B", null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    act(() => streams[1].push({ type: "delta", text: "B 답" }));
    await act(async () => {
      await a;
    });
    expect(result.current.streaming.isStreaming).toBe(true);
    expect(result.current.streaming.content).toBe("B 답");
    const [pa, pb] = result.current.pending;
    expect(pa.state).toBe("uncertain");
    expect(pb.state).toBe("sending");
    act(() => {
      streams[1].push(done("aB"));
      streams[1].close();
    });
    await act(async () => {
      await b;
    });
    expect(result.current.pending[1]).toMatchObject({ state: "confirmed", assistantId: "aB" });
  });

  it("(d1) 같은 문구 A·B, 서버엔 B만 저장, A의 늦은 GET이 uB·aB를 먼저 봄 → A는 끝까지 uncertain, B는 done(aB) 즉시 숨김", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.messages.isSuccess).toBe(true));
    let a!: Promise<unknown>;
    act(() => {
      a = result.current.send(SAME, null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    // 서버에는 B만 저장된다 — B가 A를 끊은 뒤 A의 늦은 finally가 일으킨 GET이 uB·aB를 먼저 본다
    stored = [msg("uB", "user", SAME), msg("aB", "assistant", "B 답")];
    let b!: Promise<unknown>;
    act(() => {
      b = result.current.send(SAME, null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    await act(async () => {
      await a;
    });
    await waitFor(() => expect(result.current.messages.data?.map((m) => m.id)).toEqual(["uB", "aB"]));
    // done 도착 전 겹침: 저장된 uB + A(uncertain) + B(sending) — 어느 행도 uB로 확정·제거되지 않는다
    expect(result.current.visible.map((p) => p.state)).toEqual(["uncertain", "sending"]);
    // B의 지연된 done(aB) — 이미 받은 목록에 aB가 있으므로 즉시 숨겨진다
    act(() => streams[1].push(done("aB")));
    await waitFor(() => expect(result.current.visible.map((p) => p.state)).toEqual(["uncertain"]));
    act(() => streams[1].close());
    await act(async () => {
      await b;
    });
    // 같은 GET 응답을 여러 번 반복해도 A는 uncertain, B는 다시 나타나지 않는다
    for (let i = 0; i < 3; i += 1) {
      await act(() => result.current.client.invalidateQueries({ queryKey: ["chat-messages", "s1"] }));
    }
    expect(result.current.visible).toHaveLength(1);
    expect(result.current.visible[0].state).toBe("uncertain");
  });

  it("(d2) B done(aB)이 먼저 → GET에 aB → B 숨김 → GET 반복: A는 계속 uncertain, uB가 A의 근거로 쓰이지 않음", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.messages.isSuccess).toBe(true));
    let a!: Promise<unknown>;
    act(() => {
      a = result.current.send(SAME, null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    let b!: Promise<unknown>;
    act(() => {
      b = result.current.send(SAME, null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    stored = [msg("uB", "user", SAME), msg("aB", "assistant", "B 답")];
    act(() => {
      streams[1].push(done("aB"));
      streams[1].close();
    });
    await act(async () => {
      await b;
    });
    await act(async () => {
      await a;
    });
    for (let i = 0; i < 3; i += 1) {
      await act(() => result.current.client.invalidateQueries({ queryKey: ["chat-messages", "s1"] }));
    }
    expect(result.current.visible.map((p) => [p.content, p.state])).toEqual([[SAME, "uncertain"]]);
    expect(result.current.pending.find((p) => p.state === "confirmed")?.assistantId).toBe("aB");
  });

  it("dismissPending은 그 nonce 행만 지운다", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.messages.isSuccess).toBe(true));
    let a!: Promise<unknown>;
    act(() => {
      a = result.current.send("A", null, null);
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => {
      void result.current.send("B", null, null);
    });
    await act(async () => {
      await a;
    });
    const nonceA = result.current.pending[0].nonce;
    act(() => result.current.dismissPending(nonceA));
    expect(result.current.pending.map((p) => p.content)).toEqual(["B"]);
  });
});
