import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Toaster } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAdminModels } from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";
import { FALLBACK_ERROR_DESCRIPTION } from "@/lib/notify";
import { CreateModelDialog } from "./create-model-dialog";

/**
 * 관리자 모델 생성 422(능력 enum `literal_error`, 코어 v0.18.0 #218)가 고정 문구 토스트로만 보인다
 * (core-v0.20.0-upgrade AC-1.9 ⑥). 네트워크 경계(`fetch`)만 대체한다 — api client·`useAdminModels`·
 * `notifyError`·sonner `Toaster`는 실경로다.
 */

// 코어 v0.20.0 실제 422 본문 — pytest `test_admin_model_unknown_capability_is_literal_error_422`가
// 기록한 그대로다(관리자 라우터가 `input`·`ctx`를 지운다).
const LITERAL_ERROR = {
  type: "literal_error",
  loc: ["body", "capabilities", 1],
  msg: "Input should be 'llm', 'vlm', 'embedding', 'reranker' or 'image'",
};
// 방어선 확인 — 입력값이 실려 와도(다른 경로·옛 코어) 화면에 나오지 않아야 한다
const LITERAL_ERROR_WITH_INPUT = { ...LITERAL_ERROR, input: "video-raw-input" };

const PROVIDER: AdminProvider = {
  id: "provider-1",
  name: "mockp",
  type: "mock",
  base_url: null,
  has_api_key: false,
  enabled: true,
  model_count: 0,
  embedding_max_concurrency: null,
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let posted: unknown[] = [];

function stubFetch(createBody: unknown) {
  posted = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "POST" && url.endsWith("/api/v1/admin/llm/models")) {
        posted.push(JSON.parse(String(init?.body)));
        return json(422, createBody);
      }
      if (url.includes("/available-models")) return json(200, []);
      return json(200, []);
    }),
  );
}

function Harness() {
  return <CreateModelDialog providers={[PROVIDER]} create={useAdminModels().create} />;
}

function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Toaster />
      <Harness />
    </QueryClientProvider>,
  );
}

async function submitWithCapabilities() {
  const user = userEvent.setup();
  renderDialog();
  await user.click(screen.getByRole("button", { name: "모델 추가" }));
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("combobox", { name: "프로바이더" }));
  await user.click(await screen.findByRole("option", { name: "mockp (mock)" }));
  await user.type(await within(dialog).findByLabelText("모델명 (LiteLLM 식별자)"), "m-video");
  await user.type(within(dialog).getByLabelText("표시명"), "M Video");
  await user.click(within(dialog).getByRole("button", { name: "추가" }));
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CreateModelDialog — 능력 422는 고정 문구 토스트로만 (AC-1.9 ⑥)", () => {
  it.each([
    ["코어 실제 본문(입력값 없음)", { detail: [LITERAL_ERROR] }],
    ["입력값이 실린 본문", { detail: [LITERAL_ERROR_WITH_INPUT, LITERAL_ERROR] }],
  ])("%s → 「모델을 추가하지 못했습니다」 + 고정 설명, 검증 원문 비노출", async (_label, body) => {
    stubFetch(body);
    await submitWithCapabilities();

    expect(await screen.findByText("모델을 추가하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByText(FALLBACK_ERROR_DESCRIPTION)).toBeInTheDocument();
    await waitFor(() => expect(posted).toHaveLength(1));
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("Input should be");
    expect(text).not.toContain("literal_error");
    expect(text).not.toContain("video-raw-input");
    // 다이얼로그는 열린 채 남는다(성공 때만 닫힘) — 입력을 고쳐 다시 보낼 수 있다
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
