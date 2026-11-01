import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "@/components/chat/chat-panel";
import { Providers } from "@/components/providers";
import type { ChatEntry, ChatMessage } from "@/lib/chat-types";
import { useChatPrompt } from "@/stores/chat-prompt";

// 실제 ChatPanel → 실제 훅(react-query) → 실제 api-client. 대역은 네트워크(fetch)·라우터·알림뿐이다.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/notebooks/nb1",
  useSearchParams: () => new URLSearchParams("s=s1"),
}));
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return { ...actual, toast: Object.assign(vi.fn(), { ...actual.toast, error: toastError, success: vi.fn() }) };
});

Element.prototype.scrollIntoView = vi.fn();

const BRIEF: ChatEntry = {
  agent_id: "agent-brief",
  kind: "skill",
  slash_name: "brief",
  source: "own",
  display_name: "브리핑",
  description: "짧게 요약한다",
};
const QUIZ: ChatEntry = { ...BRIEF, agent_id: "agent-quiz", slash_name: "quiz", display_name: "퀴즈" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function message(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m-q",
    session_id: "s1",
    role: "user",
    content: "질문",
    citations: [],
    model_id: null,
    token_usage: {},
    created_at: "2026-10-28T00:00:00Z",
    run_id: null,
    entry_run: null,
    ...over,
  };
}

const QUESTION = message({
  id: "m-q",
  content: "이번 주 요약해 줘",
  entry_run: { run_id: "r1", agent_id: BRIEF.agent_id, display_name: "브리핑", slash_name: "brief", status: "running" },
});
const RESULT = message({ id: "m-r", role: "assistant", content: "주간 요약 결과입니다", run_id: "r1" });

function run(status: string) {
  return {
    id: "r1",
    agent_id: BRIEF.agent_id,
    notebook_id: "nb1",
    params: {},
    trigger: "chat",
    status,
    progress: 0,
    result_summary: null,
    artifact_keys: [],
    token_usage: {},
    error: null,
    created_at: "2026-10-28T00:00:00Z",
    started_at: null,
    finished_at: null,
    can_cancel: true,
    can_hide: false,
    used_models: [],
    fallback_used: false,
  };
}

let storedMessages: ChatMessage[] = [];
let entriesResponse: () => Response = () => json([BRIEF, QUIZ]);
/** 기본 진입 응답 — 서버처럼 질문 행을 저장한 뒤 202를 준다. */
function acceptedEntry(): Response {
  const queued = { ...QUESTION, entry_run: { ...QUESTION.entry_run!, status: "queued" as const } };
  storedMessages = [...storedMessages, queued];
  return json({ user_message: queued, run: run("queued") }, 202);
}
let entryPost: (body: Record<string, unknown>) => Response = acceptedEntry;
let runResponse: () => Response = () => json(run("running"));
/** 진입 POST 응답을 늦춘다 — 테스트가 그 사이 대화를 바꾼다. */
let entryGate: Promise<void> | null = null;
/** s2 메시지의 두 번째 조회부터 멈춘다 — 잘못 붙은 캐시가 재조회로 덮이지 않고 화면에 남게 한다. */
let s2Hang: Promise<void> | null = null;
let s2Calls = 0;

const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
  const url = String(input);
  if (url.endsWith("/chat/sessions/s1/entry-runs") && init?.method === "POST") {
    if (entryGate) await entryGate;
    return entryPost(JSON.parse(String(init.body)) as Record<string, unknown>);
  }
  if (url.includes("/chat/sessions/s2/messages")) {
    s2Calls += 1;
    if (s2Calls > 1 && s2Hang) await s2Hang;
    return json([]);
  }
  if (url.includes("/chat/sessions/s1/messages") && init?.method === "POST") {
    return new Response(
      [{ type: "retrieval", citations: [] }, { type: "delta", text: "답" }, { type: "done", message_id: "m-x", citations: [], token_usage: {}, model: "m" }]
        .map((e) => `data: ${JSON.stringify(e)}\n\n`)
        .join(""),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    );
  }
  if (url.includes("/chat/sessions/s1/messages")) return json(storedMessages);
  if (url.endsWith("/api/v1/chat/entries")) return entriesResponse();
  if (url.endsWith("/agent-runs/r1")) return runResponse();
  if (url.includes("/notebooks/nb1/chat/sessions")) {
    return json([{ id: "s1", notebook_id: "nb1", title: null, pinned: false, created_at: "", updated_at: "" }]);
  }
  if (url.endsWith("/api/v1/models")) {
    return json([{ id: "m-sonnet", display_name: "claude-sonnet-5-5", is_default: true }]);
  }
  return json([]);
});

const posts = (suffix: string) =>
  fetchMock.mock.calls.filter(([u, i]) => String(u).includes(suffix) && i?.method === "POST");

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  storedMessages = [];
  entriesResponse = () => json([BRIEF, QUIZ]);
  entryPost = acceptedEntry;
  runResponse = () => json(run("running"));
  entryGate = null;
  s2Hang = null;
  s2Calls = 0;
  fetchMock.mockClear();
  toastError.mockClear();
  vi.unstubAllGlobals();
});

function renderPanel() {
  render(
    <Providers>
      <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite />
    </Providers>,
  );
}

async function typeInto(text: string) {
  const user = userEvent.setup();
  const input = screen.getByRole("textbox");
  await waitFor(() => expect(input).toHaveAttribute("placeholder", "소스에 대해 질문하세요… (/ 로 스킬 실행)"));
  await user.type(input, text);
  return { user, input };
}

describe("ChatPanel — `/` 팔레트 진입(#66 AC-10)", () => {
  it("팔레트에서 고르고 질문을 보내면 진입 실행을 요청하고, 질문 아래 run 카드가 붙는다", async () => {
    renderPanel();
    const { input } = await typeInto("/");
    const listbox = await screen.findByRole("listbox", { name: "실행할 스킬·워크플로" });
    expect(within(listbox).getAllByRole("option")).toHaveLength(2);
    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("entry-chip")).toHaveTextContent("/brief");
    await userEvent.type(input, "이번 주 요약해 줘{Enter}");

    await waitFor(() => expect(posts("/entry-runs")).toHaveLength(1));
    expect(JSON.parse(String(posts("/entry-runs")[0][1]?.body))).toEqual({
      agent_id: "agent-brief",
      content: "이번 주 요약해 줘",
      source_ids: null,
      model_id: "m-sonnet",
      idempotency_key: expect.any(String),
    });
    expect(posts("/chat/sessions/s1/messages")).toHaveLength(0); // 일반 메시지로 보내지 않았다
    const messages = await screen.findByTestId("chat-messages");
    expect(within(messages).getByText("이번 주 요약해 줘")).toBeInTheDocument();
    expect(await screen.findByTestId("entry-run-card-r1")).toHaveTextContent("브리핑");
    expect(input).toHaveValue("");
  });

  it("칩 없이 쓴 이름이 없으면(404) 알리고 입력을 그대로 둔다", async () => {
    entryPost = () => json({ detail: "Agent not found" }, 404);
    renderPanel();
    const { input } = await typeInto("/nope 질문{Enter}");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("「/nope」에 맞는 스킬·워크플로가 없습니다"));
    expect(JSON.parse(String(posts("/entry-runs")[0][1]?.body))).toMatchObject({ name: "nope", content: "질문" });
    expect(input).toHaveValue("/nope 질문");
  });

  it("이름이 여럿에 맞으면(409) 후보로 팔레트를 연다", async () => {
    entryPost = () => json({ detail: { code: "chat_entry_ambiguous", candidates: [BRIEF, QUIZ] } }, 409);
    renderPanel();
    await typeInto("/dup 질문{Enter}");
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getAllByRole("option").map((o) => o.textContent)).toEqual([
      expect.stringContaining("브리핑"),
      expect.stringContaining("퀴즈"),
    ]);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("다른 실패(활성 run 409 등)는 문구를 알리고 입력을 그대로 둔다", async () => {
    entryPost = () => json({ detail: "이 대화에서 이미 실행 중인 에이전트가 있습니다" }, 409);
    renderPanel();
    const { input } = await typeInto("/brief 질문{Enter}");
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("실행하지 못했습니다 — 이 대화에서 이미 실행 중인 에이전트가 있습니다"),
    );
    expect(input).toHaveValue("/brief 질문");
  });

  it("새로고침 뒤 끝나지 않은 진입 run에 다시 붙고, 그동안 전송을 잠근다(D3)", async () => {
    storedMessages = [QUESTION];
    renderPanel();
    expect(await screen.findByTestId("entry-run-card-r1")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("entry-run-status")).toHaveTextContent("실행 중"));
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/agent-runs/r1"))).toBe(true);
    expect(screen.getByTestId("entry-locked-hint")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox"), "끼어들기{Enter}");
    expect(posts("/chat/sessions/s1/messages")).toHaveLength(0);
  });

  it("run이 끝나면 메시지를 다시 읽어 결과를 보이고 잠금을 푼다 — 결과에는 「다시 생성」이 없다", async () => {
    storedMessages = [QUESTION];
    runResponse = () => {
      // run이 끝났다 — 다음 메시지 재조회에는 결과 행과 끝난 상태가 실린다
      storedMessages = [{ ...QUESTION, entry_run: { ...QUESTION.entry_run!, status: "succeeded" } }, RESULT];
      return json(run("succeeded"));
    };
    renderPanel();
    expect(await screen.findByText("주간 요약 결과입니다")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("entry-locked-hint")).not.toBeInTheDocument());
    expect(screen.getByTestId("entry-run-status")).toHaveTextContent("완료");
    expect(screen.queryByRole("button", { name: "다시 생성" })).not.toBeInTheDocument();
  });

  it("진입 요청 중 다른 대화로 옮기면 응답을 보낸 대화에만 반영한다(리뷰 High)", async () => {
    let open!: () => void;
    entryGate = new Promise<void>((r) => (open = r));
    s2Hang = new Promise<void>(() => {}); // 끝까지 응답하지 않는다
    const view = render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite />
      </Providers>,
    );
    await typeInto("/brief 이번 주 요약해 줘{Enter}");
    await waitFor(() => expect(posts("/chat/sessions/s1/entry-runs")).toHaveLength(1));
    view.rerender(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s2" onSessionChange={vi.fn()} canWrite />
      </Providers>,
    );
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/chat/sessions/s2/messages"))).toBe(true),
    );
    open();
    await waitFor(() => expect(storedMessages).toHaveLength(1)); // 서버는 s1에 저장했다
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("이번 주 요약해 줘")).not.toBeInTheDocument();
    expect(screen.queryByTestId("entry-run-card-r1")).not.toBeInTheDocument();
  });

  it("진입 run이 끝나기 전에는 마인드맵 질문도 보내지 않는다(D3, 리뷰 Medium)", async () => {
    storedMessages = [QUESTION];
    renderPanel();
    await screen.findByTestId("entry-locked-hint");
    useChatPrompt.getState().request({ notebookId: "nb1", text: "마인드맵 질문", sourceIds: null, origin: "mindmap" });
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("실행이 끝난 뒤 다시 눌러 주세요"));
    expect(posts("/chat/sessions/s1/messages")).toHaveLength(0);
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(0); // 취소돼 다시 누를 수 있다
  });

  it("목록을 읽지 못하면(옛 API 404) 팔레트 없이 `/` 입력을 일반 메시지로 보낸다", async () => {
    entriesResponse = () => json({ detail: "Not Found" }, 404);
    renderPanel();
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/api/v1/chat/entries"))).toBe(true),
    );
    await user.type(input, "/brief 질문");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(posts("/chat/sessions/s1/messages")).toHaveLength(1));
    expect(posts("/entry-runs")).toHaveLength(0);
  });
});
