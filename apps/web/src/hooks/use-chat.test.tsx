import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Providers } from "@/components/providers";
import { useSendMessage } from "@/hooks/use-chat";
import { PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";

// 실제 useSendMessage → 실제 api.stream → 실제 throwIfNotOk → 실제 Providers 구독.
// 대역은 네트워크(fetch)와 프레임워크 라우터(next/navigation)뿐이다(AC-3.1, v2 PR-05).
const nav = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => "/notebooks/nb1",
}));

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
  window.history.pushState({}, "", "/notebooks/nb1?s=s1");
});

afterEach(() => {
  nav.replace.mockReset();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

/** 채팅 전송 SSE 응답 — 서버 `format_sse`와 같은 `data: {json}\n\n` 프레임. */
function sseResponse(events: object[]): Response {
  return new Response(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const LLM_400 = "LLM tool 호출 실패: `temperature` is deprecated for this model.";

describe("useSendMessage — 스트림 오류 보존", () => {
  it("SSE error로 끝나면 메시지 재조회 뒤에도 오류를 남기고, 다른 대화에서는 보이지 않으며, 다음 전송이 지운다", async () => {
    let failNext = true;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/chat/sessions/s1/messages") && init?.method === "POST") {
        const retrieval = { type: "retrieval", citations: [], insufficient_context: false };
        if (failNext) {
          failNext = false;
          return sseResponse([retrieval, { type: "error", detail: LLM_400 }]);
        }
        return sseResponse([retrieval]);
      }
      // 실패한 첫 질문은 서버에 남지 않는다 — 재조회해도 빈 목록이다.
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(({ sessionId }) => useSendMessage(sessionId, "nb1"), {
      initialProps: { sessionId: "s1" },
      wrapper: ({ children }) => <Providers>{children}</Providers>,
    });

    await act(async () => {
      await result.current.send("징계 사유는?", null, null);
    });
    // finally(재조회 요청 → 스트림 비우기)를 지난 뒤에도 오류는 남았다. 재조회 자체는 메시지 쿼리를
    // 구독하는 화면이 있어야 일어나므로 chat-panel.test.tsx에서 확인한다.
    expect(result.current.streaming).toMatchObject({ isStreaming: false, content: "", error: LLM_400 });

    rerender({ sessionId: "s2" });
    expect(result.current.streaming.error).toBeNull();
    rerender({ sessionId: "s1" });
    expect(result.current.streaming.error).toBe(LLM_400);

    await act(async () => {
      await result.current.send("다시", null, null);
    });
    expect(result.current.streaming.error).toBeNull();
  });
});

describe("useSendMessage — 403 password_change_required (AC-3.1)", () => {
  it("채팅 전송이 403 code를 받으면 변경 화면으로 한 번 이동하고 훅은 오류 상태로 끝난다", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/chat/sessions/s1/messages")) {
        return new Response(
          JSON.stringify({
            detail: { code: PASSWORD_CHANGE_REQUIRED, detail: "비밀번호를 변경해야 합니다" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useSendMessage("s1", "nb1"), {
      wrapper: ({ children }) => <Providers>{children}</Providers>,
    });

    await act(async () => {
      await result.current.send("안녕", null, null);
    });

    // 전송 POST가 실제로 나갔다(대체된 경로가 아니다)
    const posted = fetchMock.mock.calls.filter(([u]) => String(u).includes("/chat/sessions/s1/messages"));
    expect(posted.length).toBeGreaterThanOrEqual(1);
    expect(nav.replace).toHaveBeenCalledTimes(1);
    expect(nav.replace).toHaveBeenCalledWith(
      `/change-password?redirect=${encodeURIComponent("/notebooks/nb1?s=s1")}`,
    );
    // 예외는 훅이 소비한다 — 그래서 QueryCache onError로는 이 경로를 잡을 수 없다(D-33)
    expect(result.current.streaming.isStreaming).toBe(false);
  });
});

describe("useSendMessage — 스킬 설치 확인 카드 (#259)", () => {
  it("제안 이벤트의 카드를 저장된 답변 id로 들고 있고, 스트림이 끝나도 지우지 않는다", async () => {
    const offer = { owner: "epoko77-ai", repo: "im-not-ai", sha: "b".repeat(40), candidates: [] };
    let call = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/chat/sessions/s1/messages") && init?.method === "POST") {
        call += 1;
        const events: object[] = [{ type: "retrieval", citations: [] }];
        if (call === 1) events.push({ type: "skill_install_offer", offer });
        events.push(
          { type: "delta", text: "요약" },
          { type: "done", message_id: `a${call}`, citations: [], token_usage: {}, model: "" },
        );
        return sseResponse(events);
      }
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useSendMessage("s1", "nb1"), {
      wrapper: ({ children }) => <Providers>{children}</Providers>,
    });
    await act(async () => {
      await result.current.send("https://github.com/epoko77-ai/im-not-ai 설치해줘", null, null);
    });
    expect(result.current.installOffers).toEqual({ a1: offer });
    expect(result.current.streaming.installOffer).toBeNull();

    // 제안이 없는 다음 전송은 카드를 더하지 않는다
    await act(async () => {
      await result.current.send("고마워", null, null);
    });
    expect(Object.keys(result.current.installOffers)).toEqual(["a1"]);
  });
});
