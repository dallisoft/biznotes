import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "@/components/chat/chat-panel";
import { Providers } from "@/components/providers";

// 실제 ChatPanel → 실제 훅(react-query) → 실제 api-client. 대역은 네트워크(fetch)와 라우터뿐이다(§3.3-3 R6).
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/notebooks/nb1",
  useSearchParams: () => new URLSearchParams("s=s1"),
}));

// jsdom에는 scrollIntoView 구현이 없다(message-list.test.tsx와 동일 처리).
Element.prototype.scrollIntoView = vi.fn();

const LLM_400 = "LLM tool 호출 실패: `temperature` is deprecated for this model.";
const QUESTION = "근로자 징계 사유에 해당하는 행위와 규정된 징계의 종류에는 어떤 것들이 있나요?";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(events: object[]): Response {
  return new Response(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

let storedMessages: unknown[] = [];
/** 테스트별로 POST 전송 응답을 바꾼다(기본: SSE error로 끝남). */
let postHandler: ((init?: RequestInit) => Response) | null = null;

/** 첫 이벤트만 보내고 끝나지 않는 SSE — 요청 signal이 abort되면 AbortError로 끊긴다(브라우저 fetch와 같다). */
function pendingSse(init?: RequestInit): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(`data: ${JSON.stringify({ type: "retrieval", citations: [] })}\n\n`),
      );
      init?.signal?.addEventListener("abort", () =>
        controller.error(new DOMException("The operation was aborted.", "AbortError")),
      );
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
  const url = String(input);
  if (url.includes("/chat/sessions/s1/messages") && init?.method === "POST") {
    if (postHandler) return postHandler(init);
    return sseResponse([
      { type: "retrieval", citations: [], insufficient_context: false },
      { type: "error", detail: LLM_400 },
    ]);
  }
  if (url.includes("/chat/sessions/s1/messages")) return json(storedMessages); // 실패한 질문은 저장되지 않는다
  if (url.includes("/notebooks/nb1/chat/sessions")) {
    return json([
      {
        id: "s1",
        notebook_id: "nb1",
        title: null,
        pinned: false,
        created_at: "2026-10-16T00:00:00Z",
        updated_at: "2026-10-16T00:00:00Z",
      },
    ]);
  }
  if (url.includes("/notebooks/nb1/overview")) {
    return json({
      summary: "취업규칙 샘플 요약",
      suggested_questions: [QUESTION],
      title_suggestions: [],
      generated_at: "2026-10-16T00:00:00Z",
      stale: false,
    });
  }
  if (url.endsWith("/api/v1/models")) {
    return json([{ id: "m-sonnet", display_name: "claude-sonnet-5-5", is_default: true }]);
  }
  return json([]);
});

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  storedMessages = [];
  postHandler = null;
  fetchMock.mockClear();
  vi.unstubAllGlobals();
});

describe("ChatPanel — 첫 질문 실패", () => {
  it("추천 질문 전송이 SSE error로 끝나 메시지가 없어도 오류와 질문(전송되지 않음)을 남긴다(깜박이고 사라지지 않는다)", async () => {
    const user = userEvent.setup();
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite={false} />
      </Providers>,
    );

    const suggestions = await screen.findByTestId("suggested-questions");
    await user.click(within(suggestions).getByRole("button", { name: QUESTION }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`답변을 받지 못했습니다 — ${LLM_400}`);
    // 전송은 실제로 나갔고(추천 질문 그대로), 화면은 빈 대화(요약·추천 질문)로 남아 있다
    const posted = fetchMock.mock.calls.find(
      ([u, i]) => String(u).includes("/chat/sessions/s1/messages") && i?.method === "POST",
    );
    expect(JSON.parse(String(posted?.[1]?.body))).toMatchObject({ content: QUESTION, autonomous: true });
    // [mindmap-agent-skill AC-2.12·HCI-16] 저장되지 않은 질문은 빈 화면으로 사라지지 않고 「전송되지 않음」으로 남는다
    const pending = await screen.findByTestId("pending-user-message");
    expect(pending).toHaveTextContent(QUESTION);
    await waitFor(() => expect(pending).toHaveTextContent("전송되지 않음"));
    // 메시지 재조회가 끝난 뒤에도 오류가 그대로 있다
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([u, i]) => String(u).includes("/chat/sessions/s1/messages") && i?.method !== "POST",
        ).length,
      ).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(LLM_400);
    // 스크롤 영역 밖(입력창 위)에 있어 소스 추가 카드 등에 밀려 접힌 아래로 내려가지 않는다
    expect(screen.getByRole("alert").closest(".overflow-y-auto")).toBeNull();
  });

  it("이전 대화가 있으면 오류를 한 번만(입력창 위) 보이고, 기존 메시지는 그대로 둔다", async () => {
    storedMessages = [
      { id: "u1", session_id: "s1", role: "user", content: "이전 질문", citations: [], seq: 1 },
      { id: "a1", session_id: "s1", role: "assistant", content: "이전 답변", citations: [], seq: 2 },
    ];
    const user = userEvent.setup();
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite={false} />
      </Providers>,
    );

    expect(await screen.findByText("이전 답변")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "새 질문{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(`답변을 받지 못했습니다 — ${LLM_400}`);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText("이전 질문")).toBeInTheDocument();
  });
});

describe("ChatPanel — 답변 중지(ui-ux-improvements B3)", () => {
  it("생성 중 「답변 중지」를 누르면 요청을 끊고, 질문을 입력창에 되돌리며, 오류를 보이지 않는다", async () => {
    postHandler = pendingSse;
    const user = userEvent.setup();
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite />
      </Providers>,
    );

    const input = await screen.findByTestId("chat-input");
    await user.type(input, "연차 규정 알려줘{Enter}");
    expect(input).toHaveValue("");

    const stop = await screen.findByRole("button", { name: "답변 중지" });
    expect(stop).toBeEnabled();
    const posted = fetchMock.mock.calls.find(
      ([u, i]) => String(u).includes("/chat/sessions/s1/messages") && i?.method === "POST",
    );
    const signal = posted?.[1]?.signal;
    expect(signal?.aborted).toBe(false);

    await user.click(stop);

    expect(signal?.aborted).toBe(true);
    await waitFor(() => expect(screen.getByTestId("chat-input")).toHaveValue("연차 규정 알려줘"));
    expect(await screen.findByRole("button", { name: "전송" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "답변 중지" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("ChatPanel — 보낸 질문 즉시 표시(ui-ux-improvements B5)", () => {
  it("답변이 오기 전에도 방금 보낸 질문 말풍선이 목록에 보인다", async () => {
    postHandler = pendingSse;
    const user = userEvent.setup();
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite />
      </Providers>,
    );
    const input = await screen.findByTestId("chat-input");
    await user.type(input, "연차 규정 알려줘{Enter}");
    const log = await screen.findByTestId("chat-messages");
    expect(await within(log).findByText("연차 규정 알려줘")).toBeInTheDocument();
  });
});

describe("ChatPanel — 다시 생성(ui-ux-improvements ③)", () => {
  it("마지막 답변의 「다시 생성」은 그 질문을 regenerate로 보내고, 생성 중에는 옛 답변 대신 새 답변 자리를 보인다", async () => {
    storedMessages = [
      { id: "u1", session_id: "s1", role: "user", content: "연차는 며칠?", citations: [], model_id: null, token_usage: {}, created_at: "2026-10-16T00:00:00Z" },
      { id: "a1", session_id: "s1", role: "assistant", content: "옛 답변", citations: [], model_id: null, token_usage: {}, created_at: "2026-10-16T00:00:01Z" },
    ];
    postHandler = pendingSse;
    const user = userEvent.setup();
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite />
      </Providers>,
    );
    await user.click(await screen.findByRole("button", { name: "다시 생성" }));

    const posted = fetchMock.mock.calls.find(
      ([u, i]) => String(u).includes("/chat/sessions/s1/messages") && i?.method === "POST",
    );
    expect(JSON.parse(String(posted?.[1]?.body))).toMatchObject({ content: "연차는 며칠?", regenerate: true });
    const log = screen.getByTestId("chat-messages");
    await waitFor(() => expect(within(log).queryByText("옛 답변")).toBeNull());
    // 질문은 한 번만 보인다(새 질문 말풍선을 덧붙이지 않는다)
    expect(within(log).getAllByText("연차는 며칠?")).toHaveLength(1);
  });
});

describe("ChatPanel — 스킬 설치 확인 카드 (#259)", () => {
  const OFFER = {
    owner: "epoko77-ai",
    repo: "im-not-ai",
    sha: "b37e0206d31fffd9149bf57168d4d62eb2086bee",
    ref: "main",
    archive_sha256: "a".repeat(64),
    archive_root: "im-not-ai-b37e0206d31fffd9149bf57168d4d62eb2086bee",
    subpath: null,
    license: { spdx_id: "MIT", name: "MIT License" },
    truncated: 0,
    max_paths: 10,
    candidates: [
      {
        path: "skills/humanize",
        name: "humanize",
        description: "윤문",
        file_count: 1,
        ok: true,
        errors: [],
        warnings: [],
        runtime_profile: "standard_agent_v1",
        has_scripts: false,
        copy_of: null,
        reinstall: null,
      },
    ],
  };
  const message = (id: string, role: "user" | "assistant", content: string) => ({
    id,
    session_id: "s1",
    role,
    content,
    citations: [],
    model_id: null,
    token_usage: {},
    provenance: null,
    created_at: "2026-10-28T00:00:00Z",
  });

  it("스트리밍 중에는 카드를 띄우지 않고, 저장된 답변 아래에만 띄운다", async () => {
    const user = userEvent.setup();
    const encoder = new TextEncoder();
    const frame = (e: object) => encoder.encode(`data: ${JSON.stringify(e)}\n\n`);
    let finish: () => void = () => {};
    postHandler = () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(frame({ type: "retrieval", citations: [] }));
            controller.enqueue(frame({ type: "skill_install_offer", offer: OFFER }));
            controller.enqueue(frame({ type: "delta", text: "후보 1개를 찾았습니다" }));
            finish = () => {
              storedMessages = [message("u1", "user", QUESTION), message("a1", "assistant", "후보 1개를 찾았습니다")];
              controller.enqueue(frame({ type: "done", message_id: "a1", citations: [], token_usage: {}, model: "" }));
              controller.close();
            };
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    render(
      <Providers>
        <ChatPanel notebookId="nb1" sessionId="s1" onSessionChange={vi.fn()} canWrite={false} />
      </Providers>,
    );
    const suggestions = await screen.findByTestId("suggested-questions");
    await user.click(within(suggestions).getByRole("button", { name: QUESTION }));

    expect(await screen.findByText("후보 1개를 찾았습니다")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-install-card")).not.toBeInTheDocument();

    finish();
    const card = await screen.findByTestId("skill-install-card");
    expect(within(card).getByTestId("skill-install-source")).toHaveTextContent("epoko77-ai/im-not-ai@b37e020");
    expect(screen.getAllByTestId("skill-install-card")).toHaveLength(1);
  });
});
