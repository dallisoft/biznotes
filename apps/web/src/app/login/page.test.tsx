import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import { LOGIN_ERROR_MESSAGE } from "@/lib/login-error";
import LoginPage from "./page";

// 네트워크 경계(`api`)·App Router만 대체한다.
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("로그인", () => {
  it("e2e 로그인 헬퍼가 쓰는 이름(이메일·비밀번호 입력, 「로그인」 버튼)은 그대로다", () => {
    renderPage();
    expect(screen.getByLabelText("이메일")).toBeInTheDocument();
    expect(screen.getByLabelText("비밀번호")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "로그인" })).toBeInTheDocument();
  });

  it("브랜드 패널의 12px 저작권 문구는 primary 면 위 /60(대비 3.0~3.2:1)이 아니라 /90이다", () => {
    renderPage();
    const copyright = screen.getByText(/biznotes — RAG 기반 노트북 서비스/);
    expect(copyright).toHaveClass("text-primary-foreground/90");
    expect(copyright.className).not.toMatch(/primary-foreground\/[0-8]0\b/);
  });
});

describe("로그인 실패 토스트 — 서버 메시지·입력값을 보이지 않는다(#93)", () => {
  it("검증 422 본문의 input(비밀번호)이 토스트 어디에도 없다", async () => {
    const detail = [
      { loc: ["body", "password"], msg: "String should have at most 128 characters", input: "s3cret-pw" },
    ];
    api.post.mockRejectedValue(new ApiError(422, JSON.stringify(detail), { detail }));
    renderPage();

    await userEvent.type(screen.getByLabelText("이메일"), "a@b.co");
    await userEvent.type(screen.getByLabelText("비밀번호"), "s3cret-pw");
    await userEvent.click(screen.getByRole("button", { name: "로그인" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    const [title, options] = vi.mocked(toast.error).mock.calls[0];
    expect(title).toBe("로그인하지 못했습니다");
    expect(options).toMatchObject({ description: LOGIN_ERROR_MESSAGE.invalidInput });
    expect(JSON.stringify(vi.mocked(toast.error).mock.calls)).not.toContain("s3cret-pw");
  });

  it("401은 서버 영문 메시지 대신 사전 문구를 보인다", async () => {
    api.post.mockRejectedValue(new ApiError(401, "Invalid email or password"));
    renderPage();

    await userEvent.type(screen.getByLabelText("이메일"), "a@b.co");
    await userEvent.type(screen.getByLabelText("비밀번호"), "wrong-pw");
    await userEvent.click(screen.getByRole("button", { name: "로그인" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(vi.mocked(toast.error).mock.calls[0][1]).toMatchObject({
      description: LOGIN_ERROR_MESSAGE.invalidCredentials,
    });
    expect(JSON.stringify(vi.mocked(toast.error).mock.calls)).not.toContain("Invalid email");
  });
});
