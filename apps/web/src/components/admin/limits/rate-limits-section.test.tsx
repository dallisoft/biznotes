import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RateLimitsSection } from "@/components/admin/limits/rate-limits-section";
import { ApiError } from "@/lib/api-client";
import type { RateLimits } from "@/lib/admin-types";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { qk } from "@/lib/query-keys";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

// API 경계만 mock — `api` 객체만 대체하고 ApiError 등은 실제 모듈을 쓴다.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const PATH = "/api/v1/admin/rate-limits";

/** 코어 v0.26.0 `RateLimitsOut` — 채팅만 재정의가 있고, 에이전트 실행은 env에서 끈 상태(0). */
function rateLimits(overrides: Partial<Record<string, number | null>> = {}, enabled = true): RateLimits {
  const env: Record<string, number> = {
    rate_limit_chat: 30,
    rate_limit_source_create: 30,
    rate_limit_source_upload: 1000,
    rate_limit_web_search: 30,
    rate_limit_search: 120,
    rate_limit_source_content: 60,
    rate_limit_agent_run: 0,
  };
  const override: Record<string, number | null> = { rate_limit_chat: 12, ...overrides };
  return {
    enabled,
    cache_ttl_seconds: 30,
    min_value: 1,
    max_value: 100_000,
    items: Object.entries(env).map(([key, env_value]) => ({
      key: key as RateLimits["items"][number]["key"],
      env_value,
      override: override[key] ?? null,
      effective: override[key] ?? env_value,
    })),
  };
}

function renderSection(data: RateLimits = rateLimits()) {
  api.get.mockImplementation((p: string) =>
    p === PATH ? Promise.resolve(data) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RateLimitsSection />
    </QueryClientProvider>,
  );
  return { queryClient };
}

const section = () => screen.getByRole("region", { name: "분당 레이트 리밋" });
const row = (key: string) => screen.getByTestId(`rate-limit-${key}`);
const saveButton = () => within(section()).getByRole("button", { name: "레이트 리밋 저장" });

afterEach(() => {
  vi.resetAllMocks();
});

describe("RateLimitsSection — 분당 레이트 리밋(코어 #386, #228 AC-12)", () => {
  it("7개 키를 한국어 라벨로, 키마다 env 기본값·재정의·적용 값을 나눠 보인다", async () => {
    renderSection();
    expect(await screen.findByLabelText("채팅 질문 재정의")).toHaveValue(12);
    expect(within(section()).getAllByRole("row").slice(1)).toHaveLength(7);
    for (const label of [
      "채팅 질문",
      "에이전트 실행",
      "검색",
      "웹 검색",
      "소스 등록",
      "파일 업로드",
      "소스 원문 조회",
    ]) {
      expect(screen.getByLabelText(`${label} 재정의`)).toBeInTheDocument();
    }
    // 재정의가 있는 키: env 30 · 재정의 12 · 적용 12(재정의)
    expect(within(row("rate_limit_chat")).getByTestId("env-value")).toHaveTextContent("30");
    expect(within(row("rate_limit_chat")).getByTestId("effective-value")).toHaveTextContent("12재정의");
    // 재정의가 없는 키: 입력 빈칸, 적용 값 = env 값(env)
    expect(screen.getByLabelText("검색 재정의")).toHaveValue(null);
    expect(within(row("rate_limit_search")).getByTestId("effective-value")).toHaveTextContent("120env");
    // env에서 끈 키(0 이하)는 「꺼짐」
    expect(within(row("rate_limit_agent_run")).getByTestId("env-value")).toHaveTextContent("꺼짐");
    // 전체 켜기·끄기는 env 전용(읽기만) · 다른 프로세스 반영 지연(cache_ttl_seconds)
    expect(screen.getByTestId("rate-limit-enabled")).toHaveTextContent("켜짐");
    expect(screen.getByTestId("rate-limit-enabled")).toHaveTextContent("RATE_LIMIT_ENABLED");
    expect(section()).toHaveTextContent("다른 API 프로세스에는 최대 30초 뒤에 반영됩니다");
    expect(section()).toHaveTextContent("재정의 범위 1~100,000회/분");
    expect(saveButton()).toBeDisabled();
  });

  it("저장은 바꾼 키만 보내고(부분 갱신), 응답을 새 기준으로 그린다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(rateLimits({ rate_limit_search: 200 }));
    const input = await screen.findByLabelText("검색 재정의");

    await user.type(input, "200");
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith(PATH, { rate_limit_search: 200 });
    expect(toast.success).toHaveBeenCalledWith(
      "분당 레이트 리밋을 저장했습니다",
      expect.objectContaining({ description: "다른 API 프로세스에는 최대 30초 뒤에 반영됩니다" }),
    );
    await waitFor(() =>
      expect(within(row("rate_limit_search")).getByTestId("effective-value")).toHaveTextContent("200재정의"),
    );
    expect(saveButton()).toBeDisabled();
    // 저장은 화면 전체 상태를 다시 읽지 않는다(PUT 응답이 전체 상태다)
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("저장 중에 더 고친 칸은 저장 응답·재조회가 덮지 않는다 — 편집하지 않은 칸만 서버 값을 따른다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSection();
    let resolve!: (saved: RateLimits) => void;
    api.put.mockReturnValue(new Promise<RateLimits>((r) => (resolve = r)));
    const search = await screen.findByLabelText("검색 재정의");
    await user.type(search, "200");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { rate_limit_search: 200 }));

    // 저장 중에 보낸 칸(검색)을 더 고치고, 다른 칸(채팅)도 고친다
    const chat = screen.getByLabelText("채팅 질문 재정의");
    await user.type(search, "0");
    await user.clear(chat);
    await user.type(chat, "15");
    await act(async () => resolve(rateLimits({ rate_limit_search: 200 })));

    await waitFor(() =>
      expect(within(row("rate_limit_search")).getByTestId("effective-value")).toHaveTextContent("200재정의"),
    );
    expect(search).toHaveValue(2000);
    expect(chat).toHaveValue(15);
    // 재조회로 서버 값이 바뀌어도 편집한 칸은 그대로, 편집하지 않은 칸(웹 검색)만 새 값
    act(() => {
      queryClient.setQueryData(
        qk.adminCoreLimits.rateLimits,
        rateLimits({ rate_limit_search: 200, rate_limit_web_search: 77 }),
      );
    });
    await waitFor(() => expect(screen.getByLabelText("웹 검색 재정의")).toHaveValue(77));
    expect(search).toHaveValue(2000);
    expect(chat).toHaveValue(15);
    expect(saveButton()).toBeEnabled();
  });

  it("「지우기」 뒤 저장하면 그 키에 null을 보내 env 값으로 돌린다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(rateLimits({ rate_limit_chat: null }));
    await screen.findByLabelText("채팅 질문 재정의");

    await user.click(screen.getByRole("button", { name: "채팅 질문 재정의 지우기" }));
    expect(screen.getByLabelText("채팅 질문 재정의")).toHaveValue(null);
    expect(within(row("rate_limit_chat")).getByText("저장하면 env 값으로 돌아갑니다")).toBeInTheDocument();
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { rate_limit_chat: null }));
    await waitFor(() =>
      expect(within(row("rate_limit_chat")).getByTestId("effective-value")).toHaveTextContent("30env"),
    );
  });

  it("「변경 취소」는 저장하지 않고 저장된 값으로 돌린다", async () => {
    const user = userEvent.setup();
    renderSection();
    const input = await screen.findByLabelText("채팅 질문 재정의");
    await user.clear(input);
    await user.type(input, "99");
    await user.click(within(section()).getByRole("button", { name: "변경 취소" }));
    expect(input).toHaveValue(12);
    expect(within(section()).queryByRole("button", { name: "변경 취소" })).toBeNull();
    expect(api.put).not.toHaveBeenCalled();
  });

  it("범위 밖 값(0 — 끄기는 env 전용)은 보내지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    const input = await screen.findByLabelText("채팅 질문 재정의");
    await user.clear(input);
    await user.type(input, "0");
    await user.click(saveButton());
    expect(api.put).not.toHaveBeenCalled();
  });

  it("422는 서버가 준 범위로 항목별 문구를 보이고, 오류 토스트를 띄우지 않으며 입력을 남긴다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockRejectedValue(
      new ApiError(422, "[]", {
        detail: [
          {
            type: "less_than_equal",
            loc: ["body", "rate_limit_chat"],
            msg: "Input should be less than or equal to 50000",
            input: 60_000,
            ctx: { le: 50_000 },
          },
        ],
      }),
    );
    const input = await screen.findByLabelText("채팅 질문 재정의");
    await user.clear(input);
    await user.type(input, "60000");
    await user.click(saveButton());

    const alert = await within(section()).findByRole("alert");
    expect(alert).toHaveTextContent("저장하지 못했습니다");
    expect(alert).toHaveTextContent("채팅 질문: 50,000 이하여야 합니다");
    expect(alert).not.toHaveTextContent("60000");
    expect(toast.error).not.toHaveBeenCalled();
    expect(input).toHaveValue(60_000);
    // 다시 고치면 오류 문구를 지운다
    await user.type(input, "{Backspace}");
    expect(within(section()).queryByRole("alert")).toBeNull();
  });

  it("422가 아닌 실패는 오류 토스트로 알린다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockRejectedValue(new ApiError(500, "boom"));
    await user.type(await screen.findByLabelText("검색 재정의"), "5");
    await user.click(saveButton());
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][0]).toBe("분당 레이트 리밋을 저장하지 못했습니다");
    expect(within(section()).queryByRole("alert")).toBeNull();
  });

  it("레이트 리밋이 전체 꺼져 있으면(RATE_LIMIT_ENABLED=false) 저장해도 적용되지 않는다고 알린다", async () => {
    renderSection(rateLimits({}, false));
    const notice = await screen.findByTestId("rate-limit-enabled");
    expect(notice).toHaveTextContent("레이트 리밋이 전체 꺼져 있습니다");
    expect(notice).toHaveTextContent("아래 값을 저장해도 적용되지 않습니다");
  });

  it("불러오기에 실패하면 섹션 안에 오류와 다시 시도를 보인다", async () => {
    const user = userEvent.setup();
    let fail = true;
    api.get.mockImplementation(() => (fail ? Promise.reject(new Error("down")) : Promise.resolve(rateLimits())));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <RateLimitsSection />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("분당 레이트 리밋을 불러오지 못했습니다")).toBeInTheDocument();
    fail = false;
    await user.click(within(section()).getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByLabelText("채팅 질문 재정의")).toHaveValue(12);
  });
});
