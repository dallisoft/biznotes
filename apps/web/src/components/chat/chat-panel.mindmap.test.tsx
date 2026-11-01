/**
 * 마인드맵 질문 소비(AC-2.4·2.5·2.6)와 대기 user 메시지(AC-2.12 ①~③·(a)~(c)).
 *
 * 실제 ChatPanel → 실제 useSendMessage → 실제 api-client. 대역은 fetch(지연 SSE 포함)·라우터·토스트뿐이다(§12 R-1·R-2).
 * 겹친 전송((d)·(d1)·(d2))은 UI에서 만들 수 없어(스트리밍 중 전송은 막힌다) `use-chat.pending.test.tsx`가 훅 경로로 본다.
 */
import { QueryClient, useQueryClient } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "@/components/chat/chat-panel";
import { Providers } from "@/components/providers";
import { useChatPrompt } from "@/stores/chat-prompt";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/notebooks/nb1",
  useSearchParams: () => new URLSearchParams("s=s1"),
}));
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return { ...actual, toast: { ...actual.toast, error: toastError, success: vi.fn() } };
});

Element.prototype.scrollIntoView = vi.fn();

const Q = "이 소스들이 「에스프레소」에 대해 무엇을 말하는지 「커피」의 맥락에서 설명해 주세요.";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** 지연 SSE — 테스트가 이벤트를 한 개씩 흘리고 닫거나 끊는다. abort 신호가 오면 읽기가 AbortError로 끝난다. */
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
    fail: () => ctrl.error(new TypeError("network down")),
  };
}

type Sse = ReturnType<typeof controlledSse>;
const posts: { url: string; body: Record<string, unknown>; sse: Sse | null }[] = [];
let stored: Record<string, unknown[]> = {};
let postMode: "controlled" | { status: number } = "controlled";
let sessionsFor: Record<string, unknown[] | "never"> = {};

function sessionRow(id: string, nb: string) {
  return { id, notebook_id: nb, title: "대화", pinned: false, created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z" };
}

const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
  const url = String(input);
  const m = url.match(/\/chat\/sessions\/([^/]+)\/messages/);
  if (m && init?.method === "POST") {
    const body = JSON.parse(String(init.body));
    if (postMode !== "controlled") {
      posts.push({ url, body, sse: null });
      return json({ detail: "한도 초과" }, postMode.status);
    }
    const sse = controlledSse(init.signal);
    posts.push({ url, body, sse });
    return sse.response;
  }
  if (m) return json(stored[m[1]] ?? []);
  const nb = url.match(/\/notebooks\/([^/]+)\/chat\/sessions/);
  if (nb) {
    const rows = sessionsFor[nb[1]];
    if (rows === "never") return new Promise<Response>(() => undefined);
    return json(rows ?? [sessionRow("s1", nb[1])]);
  }
  if (url.includes("/overview")) {
    return json({ summary: "요약", suggested_questions: [], title_suggestions: [], generated_at: "2026-10-16T00:00:00Z", stale: false });
  }
  if (url.endsWith("/api/v1/models")) return json([{ id: "m1", display_name: "모델", is_default: true }]);
  return json([]);
});

function msg(id: string, role: "user" | "assistant", content: string, extra: Record<string, unknown> = {}) {
  return { id, session_id: "s1", role, content, citations: [], model_id: null, token_usage: {}, created_at: "2026-10-16T00:00:00Z", ...extra };
}

const CITES = [1, 2].map((n) => ({
  n,
  source_id: `src${n}`,
  chunk_id: `c${n}`,
  quote: `발췌 ${n}`,
  source_title: `소스 ${n}`,
}));

function Panel({ notebookId = "nb1", sessionId = "s1" as string | null }) {
  return (
    <ChatPanel notebookId={notebookId} sessionId={sessionId} onSessionChange={vi.fn()} canWrite={false} />
  );
}

function renderPanel(props: { notebookId?: string; sessionId?: string | null } = {}) {
  const utils = render(
    <Providers>
      <Panel {...props} />
    </Providers>,
  );
  return {
    ...utils,
    rerenderPanel: (next: { notebookId?: string; sessionId?: string | null }) =>
      utils.rerender(
        <Providers>
          <Panel {...next} />
        </Providers>,
      ),
  };
}

/** QueryClient에 접근해야 하는 테스트용 — Providers 안에서 클라이언트를 붙잡는다. */
function renderPanelWithClient() {
  let captured: QueryClient | null = null;
  function Grab() {
    captured = useQueryClient();
    return null;
  }
  render(
    <Providers>
      <Grab />
      <Panel />
    </Providers>,
  );
  return { client: () => captured as unknown as QueryClient };
}

function requestPrompt(notebookId = "nb1", text = Q) {
  let nonce: string | null = null;
  act(() => {
    nonce = useChatPrompt.getState().request({ notebookId, text, sourceIds: ["src1"], origin: "mindmap" });
  });
  return nonce as string | null;
}

const getCount = (sid: string) =>
  fetchMock.mock.calls.filter(([u, i]) => String(u).includes(`/chat/sessions/${sid}/messages`) && i?.method !== "POST").length;

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })),
  );
  vi.stubGlobal("fetch", fetchMock);
  posts.length = 0;
  stored = {};
  postMode = "controlled";
  sessionsFor = {};
  toastError.mockReset();
  useChatPrompt.setState({ requests: {}, busyNonce: null });
});

afterEach(() => {
  fetchMock.mockClear();
  vi.unstubAllGlobals();
});

describe("chat-prompt store (AC-2.1) — 소비 컴포넌트와 함께", () => {
  it("같은 노트북의 만료 전 미소비 요청이 있으면 새 요청 거부, 만료된 요청은 정리하고 받는다, busy는 같은 nonce로만 해제", () => {
    const store = useChatPrompt.getState();
    const a = store.request({ notebookId: "nbX", text: "a", sourceIds: null, origin: "mindmap" });
    expect(a).not.toBeNull();
    expect(useChatPrompt.getState().request({ notebookId: "nbX", text: "b", sourceIds: null, origin: "mindmap" })).toBeNull();
    // 다른 노트북 요청은 막지 않는다
    expect(useChatPrompt.getState().request({ notebookId: "nbY", text: "c", sourceIds: null, origin: "mindmap" })).not.toBeNull();
    // a를 만료시킨다
    useChatPrompt.setState((s) => ({ requests: { ...s.requests, [a as string]: { ...s.requests[a as string], expiresAt: Date.now() - 1 } } }));
    expect(useChatPrompt.getState().claim(a as string)).toBe(false);
    const d = useChatPrompt.getState().request({ notebookId: "nbX", text: "d", sourceIds: null, origin: "mindmap" });
    expect(d).not.toBeNull();
    expect(useChatPrompt.getState().requests[a as string]).toBeUndefined();
    useChatPrompt.getState().setBusy("n1");
    useChatPrompt.getState().releaseBusy("n2");
    expect(useChatPrompt.getState().busyNonce).toBe("n1");
    useChatPrompt.getState().releaseBusy("n1");
    expect(useChatPrompt.getState().busyNonce).toBeNull();
  });
});

describe("ChatPanel — 마인드맵 질문 소비(AC-2.4·2.5·2.6)", () => {
  it("mindmap_send_once: 요청 → 정확히 1회 전송, 본문 = {content, source_ids, model_id, autonomous:false, origin:mindmap}; 끝나면 busy 해제·소비", async () => {
    renderPanel();
    await screen.findByTestId("conversation-bar");
    await waitFor(() => expect(getCount("s1")).toBeGreaterThan(0));
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(useChatPrompt.getState().busyNonce).not.toBeNull();
    const body = posts[0].body;
    expect(Object.keys(body).sort()).toEqual(["autonomous", "content", "model_id", "origin", "source_ids"]);
    expect(body).toMatchObject({ content: Q, source_ids: ["src1"], autonomous: false, origin: "mindmap" });

    // ① done 전에 질문이 정확히 1번 보인다 + 「마인드맵 질문」 표시
    const pending = await screen.findByTestId("pending-user-message");
    expect(pending).toHaveTextContent(Q);
    expect(within(pending).getByTestId("mindmap-prompt-badge")).toBeInTheDocument();
    expect(screen.getAllByText(Q)).toHaveLength(1);

    // 정적 경로 SSE — retrieval(n=1,2) → delta → done
    stored.s1 = [
      msg("u1", "user", Q, { token_usage: { mindmap_prompt: 1 } }),
      msg("a1", "assistant", "에스프레소는 압력으로 추출한다 [1][2]", { citations: CITES }),
    ];
    act(() => {
      posts[0].sse?.push({ type: "retrieval", citations: CITES, insufficient_context: false });
      posts[0].sse?.push({ type: "delta", text: "에스프레소는 압력으로 추출한다 [1][2]" });
      posts[0].sse?.push({ type: "done", message_id: "a1", citations: CITES, token_usage: {}, model: "모델", insufficient_context: false, provenance: null });
      posts[0].sse?.close();
    });
    // ② done → 재조회 뒤 같은 질문 정확히 1번, 대기 행 0
    await waitFor(() => expect(useChatPrompt.getState().busyNonce).toBeNull());
    await waitFor(() => expect(screen.queryByTestId("pending-user-message")).toBeNull());
    expect(screen.getAllByText(Q)).toHaveLength(1);
    expect(screen.getByTestId("mindmap-prompt-badge")).toBeInTheDocument();
    // 인용 칩 [1]·[2]
    const messages = screen.getByTestId("chat-messages");
    expect(within(messages).getByRole("button", { name: "1" })).toBeInTheDocument();
    expect(within(messages).getByRole("button", { name: "2" })).toBeInTheDocument();
    expect(useChatPrompt.getState().requests).toEqual({});
    expect(posts).toHaveLength(1);
  });

  it("일반 질문의 요청 본문은 그대로다(origin 키 없음, autonomous:true)", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByTestId("conversation-bar");
    await user.type(screen.getByRole("textbox"), "일반 질문{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).not.toHaveProperty("origin");
    expect(posts[0].body).toMatchObject({ content: "일반 질문", autonomous: true });
  });

  it("근거 부족이면 기존 배너", async () => {
    renderPanel();
    await screen.findByTestId("conversation-bar");
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(1));
    act(() => {
      posts[0].sse?.push({ type: "retrieval", citations: [], insufficient_context: true });
      posts[0].sse?.push({ type: "delta", text: "근거가 부족합니다" });
    });
    expect(await screen.findByText(/근거가 부족/)).toBeInTheDocument();
    expect(screen.getByTestId("insufficient-context-banner")).toBeInTheDocument();
  });

  it("① 노트북 A에서 세션 대기 중 B로 이동 → B 요청만 정확히 1회 전송, A 요청은 0", async () => {
    sessionsFor = { nbA: "never" };
    const { rerenderPanel } = renderPanel({ notebookId: "nbA", sessionId: null });
    const a = requestPrompt("nbA", "A 질문");
    await waitFor(() => expect(useChatPrompt.getState().requests[a as string]?.claimed).toBe(true));
    rerenderPanel({ notebookId: "nbB", sessionId: "sB" });
    await waitFor(() => expect(useChatPrompt.getState().requests[a as string]).toBeUndefined());
    expect(useChatPrompt.getState().busyNonce).toBeNull();
    requestPrompt("nbB", "B 질문");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toContain("/chat/sessions/sB/messages");
    expect(posts[0].body.content).toBe("B 질문");
    await new Promise((r) => setTimeout(r, 30));
    expect(posts.filter((p) => p.body.content === "A 질문")).toHaveLength(0);
  });

  it("② 스트림 오류 뒤 다시 누르면 1회 더 전송한다(교착 없음)", async () => {
    renderPanel();
    await screen.findByTestId("conversation-bar");
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(1));
    act(() => posts[0].sse?.fail());
    await waitFor(() => expect(useChatPrompt.getState().busyNonce).toBeNull());
    expect(useChatPrompt.getState().requests).toEqual({});
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(2));
  });

  it("스트리밍 중이면 거부 토스트 후 그 요청만 취소한다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByTestId("conversation-bar");
    await user.type(screen.getByRole("textbox"), "일반 질문{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    requestPrompt();
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("답변이 끝난 뒤 다시 눌러 주세요"));
    expect(useChatPrompt.getState().requests).toEqual({});
    expect(useChatPrompt.getState().busyNonce).toBeNull();
    expect(posts).toHaveLength(1);
  });

  it("세션이 만료 전까지 준비되지 않으면 토스트 후 취소(전송 0)", async () => {
    sessionsFor = { nb1: "never" };
    renderPanel({ sessionId: null });
    act(() => {
      useChatPrompt.setState({
        requests: {
          n1: { nonce: "n1", notebookId: "nb1", text: Q, sourceIds: null, origin: "mindmap", expiresAt: Date.now() + 40, claimed: false },
        },
      });
    });
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("대화를 준비하지 못해 질문을 보내지 못했습니다"));
    expect(useChatPrompt.getState().requests).toEqual({});
    expect(useChatPrompt.getState().busyNonce).toBeNull();
    expect(posts).toHaveLength(0);
  });

  it("④ 이전 ChatPanel의 지연 cleanup은 자기 nonce만 취소하고 새 busy nonce를 해제하지 않는다", async () => {
    sessionsFor = { nb1: "never" };
    const first = renderPanel({ sessionId: null });
    const n1 = requestPrompt();
    await waitFor(() => expect(useChatPrompt.getState().requests[n1 as string]?.claimed).toBe(true));
    act(() => useChatPrompt.getState().setBusy("n2"));
    first.unmount();
    expect(useChatPrompt.getState().requests[n1 as string]).toBeUndefined();
    expect(useChatPrompt.getState().busyNonce).toBe("n2");
  });

  it("③ 전송 중 재마운트(768px 경계)해도 중복 전송 0, 끝나면 다음 요청을 받는다(교착 0)", async () => {
    const first = renderPanel();
    await screen.findByTestId("conversation-bar");
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(1));
    first.unmount();
    renderPanel();
    await screen.findByTestId("conversation-bar");
    await new Promise((r) => setTimeout(r, 30));
    expect(posts).toHaveLength(1);
    act(() => {
      posts[0].sse?.push({ type: "done", message_id: "a1", citations: [], token_usage: {}, model: "모델", provenance: null });
      posts[0].sse?.close();
    });
    await waitFor(() => expect(useChatPrompt.getState().busyNonce).toBeNull());
    requestPrompt();
    await waitFor(() => expect(posts).toHaveLength(2));
  });
});

describe("ChatPanel — 대기 user 메시지(AC-2.12)", () => {
  async function sendPlain(text = "질문 하나") {
    const user = userEvent.setup();
    await screen.findByTestId("conversation-bar");
    await user.type(screen.getByRole("textbox"), `${text}{Enter}`);
    await waitFor(() => expect(posts.length).toBeGreaterThan(0));
    return posts[posts.length - 1];
  }

  it("확정된 대기 행은 저장 목록에서 한 번 확인되면 사라지고, 그 답변이 뒤에 바뀌어도(다시 생성) 되살아나지 않는다", async () => {
    const { client } = renderPanelWithClient();
    const post = await sendPlain("R 질문");
    stored.s1 = [msg("u1", "user", "R 질문"), msg("a1", "assistant", "첫 답")];
    act(() => {
      post.sse?.push({ type: "done", message_id: "a1", citations: [], token_usage: {}, model: "모델", provenance: null });
      post.sse?.close();
    });
    await waitFor(() => expect(screen.queryByTestId("pending-user-message")).toBeNull());
    // 다시 생성으로 a1이 a2로 바뀐다
    stored.s1 = [msg("u2", "user", "R 질문"), msg("a2", "assistant", "새 답")];
    await act(() => client().invalidateQueries({ queryKey: ["chat-messages", "s1"] }));
    await screen.findByText("새 답");
    expect(screen.queryByTestId("pending-user-message")).toBeNull();
    expect(screen.getAllByText("R 질문")).toHaveLength(1);
  });

  it("③ 다른 대화로 옮기면 그 대기 메시지는 보이지 않는다", async () => {
    const { rerenderPanel } = renderPanel();
    await sendPlain("세션 1 질문");
    expect(await screen.findByTestId("pending-user-message")).toHaveTextContent("세션 1 질문");
    rerenderPanel({ sessionId: "s2" });
    await waitFor(() => expect(screen.queryByTestId("pending-user-message")).toBeNull());
  });

  it("(a) done 뒤 error + 저장됨 → 저장 질문 1개, 「전송되지 않음」 0, 「응답 이후 처리 오류」", async () => {
    renderPanel();
    const post = await sendPlain("A 질문");
    stored.s1 = [msg("u1", "user", "A 질문"), msg("a1", "assistant", "답")];
    act(() => {
      post.sse?.push({ type: "delta", text: "답" });
      post.sse?.push({ type: "done", message_id: "a1", citations: [], token_usage: {}, model: "모델", provenance: null });
      post.sse?.push({ type: "error", detail: "사용량 기록 실패" });
      post.sse?.close();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("응답 이후 처리 오류 — 사용량 기록 실패");
    await waitFor(() => expect(screen.queryByTestId("pending-user-message")).toBeNull());
    expect(screen.getAllByText("A 질문")).toHaveLength(1);
    expect(screen.queryByText("전송되지 않음")).toBeNull();
  });

  it("(b) done 전 reader 실패 + 이미 저장됨 → 「전송되지 않음」 0, 저장 질문 1 + 「전송 결과 확인 필요」 행 1(닫기 가능)", async () => {
    renderPanel();
    const post = await sendPlain("B 질문");
    stored.s1 = [msg("u1", "user", "B 질문"), msg("a1", "assistant", "답")];
    act(() => post.sse?.fail());
    const pending = await screen.findByTestId("pending-user-message");
    await waitFor(() => expect(pending).toHaveTextContent("응답 중단 — 전송 결과 확인 필요"));
    await waitFor(() => expect(screen.getAllByText("B 질문")).toHaveLength(2));
    expect(screen.queryByText("전송되지 않음")).toBeNull();
    await userEvent.setup().click(within(pending).getByTestId("pending-dismiss"));
    expect(screen.queryByTestId("pending-user-message")).toBeNull();
    expect(screen.getAllByText("B 질문")).toHaveLength(1);
  });

  it("(c) 4xx(429) → 원문 + 「전송되지 않음」 + 오류 문구", async () => {
    postMode = { status: 429 };
    renderPanel();
    await sendPlain("C 질문");
    const pending = await screen.findByTestId("pending-user-message");
    await waitFor(() => expect(pending).toHaveTextContent("전송되지 않음"));
    expect(pending).toHaveTextContent("C 질문");
    expect(await screen.findByRole("alert")).toHaveTextContent("한도 초과");
  });

  it("(c) done 없는 SSE error + 종료 뒤 새 메시지 0 → 「전송되지 않음」, 새 메시지 있음 → 「확인 필요」", async () => {
    renderPanel();
    const p1 = await sendPlain("D 질문");
    act(() => {
      p1.sse?.push({ type: "error", detail: "LLM 실패" });
      p1.sse?.close();
    });
    const pending = await screen.findByTestId("pending-user-message");
    await waitFor(() => expect(pending).toHaveAttribute("data-pending-state", "not_saved"));
    await userEvent.setup().click(within(pending).getByTestId("pending-dismiss"));

    const p2 = await sendPlain("E 질문");
    stored.s1 = [msg("ux", "user", "다른 전송"), msg("ax", "assistant", "다른 답")];
    act(() => {
      p2.sse?.push({ type: "error", detail: "LLM 실패" });
      p2.sse?.close();
    });
    await waitFor(() =>
      expect(screen.getByTestId("pending-user-message")).toHaveAttribute("data-pending-state", "uncertain"),
    );
  });
});
