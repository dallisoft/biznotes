import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceUploadSection } from "@/components/admin/limits/source-upload-section";
import { ApiError } from "@/lib/api-client";
import type { SourceUploadSettings } from "@/lib/admin-types";
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

const PATH = "/api/v1/admin/source-upload";
const MIB = 1024 * 1024;

function settings(override: number | null, env = 50 * MIB): SourceUploadSettings {
  return {
    max_upload_bytes: override ?? env,
    env_max_upload_bytes: env,
    override,
    hard_max_upload_bytes: 500 * MIB,
  };
}

function renderSection(data: SourceUploadSettings = settings(10 * MIB)) {
  api.get.mockImplementation((p: string) =>
    p === PATH ? Promise.resolve(data) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SourceUploadSection />
    </QueryClientProvider>,
  );
  return { queryClient };
}

const section = () => screen.getByRole("region", { name: "업로드 최대 크기" });
const input = () => screen.findByLabelText("업로드 최대 크기 재정의");
const saveButton = () => within(section()).getByRole("button", { name: "업로드 크기 저장" });
const proxyHint = () => screen.queryByTestId("upload-proxy-hint");

afterEach(() => {
  vi.resetAllMocks();
});

describe("SourceUploadSection — 업로드 최대 크기(코어 #388, #228 AC-12·D4)", () => {
  it("env 기본값과 재정의·적용 값을 MB로 나눠 보인다", async () => {
    renderSection();
    expect(await input()).toHaveValue(10);
    expect(screen.getByTestId("upload-env-value")).toHaveTextContent("50MB");
    expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("10MB재정의");
    expect(await input()).toHaveAccessibleDescription(expect.stringContaining("범위 1바이트~500MB"));
    expect(screen.getByText("MB")).toBeInTheDocument();
    expect(proxyHint()).not.toBeNull();
    expect(saveButton()).toBeDisabled();
  });

  it("재정의가 없으면 입력은 빈칸이고 적용 값은 env 값이다", async () => {
    renderSection(settings(null));
    expect(await input()).toHaveValue(null);
    expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("50MBenv");
  });

  it("저장은 MB를 바이트로 바꿔 보내고, 업로드 다이얼로그의 한도 조회를 무효화한다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSection();
    queryClient.setQueryData(qk.uploadLimits.all, { max_upload_bytes: 10 * MIB });
    api.put.mockResolvedValue(settings(80 * MIB));

    const field = await input();
    await user.clear(field);
    await user.type(field, "80");
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: 80 * MIB }));
    expect(toast.success).toHaveBeenCalledWith("업로드 최대 크기를 저장했습니다", expect.anything());
    expect(queryClient.getQueryState(qk.uploadLimits.all)?.isInvalidated).toBe(true);
    await waitFor(() => expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("80MB재정의"));
  });

  it("저장 중에 다시 고친 값은 저장 응답이 덮지 않고, 편집하지 않았으면 재조회 값을 따른다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSection();
    let resolve!: (saved: SourceUploadSettings) => void;
    api.put.mockReturnValue(new Promise<SourceUploadSettings>((r) => (resolve = r)));
    const field = await input();
    await user.clear(field);
    await user.type(field, "80");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: 80 * MIB }));

    await user.type(field, "5");
    await act(async () => resolve(settings(80 * MIB)));
    await waitFor(() => expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("80MB재정의"));
    expect(field).toHaveValue(805);
    expect(saveButton()).toBeEnabled();

    // 「변경 취소」로 편집을 버리면 재조회 값을 그대로 따른다
    await user.click(within(section()).getByRole("button", { name: "변경 취소" }));
    act(() => {
      queryClient.setQueryData(qk.adminCoreLimits.sourceUpload, settings(30 * MIB));
    });
    await waitFor(() => expect(field).toHaveValue(30));
  });

  it("「지우기」 뒤 저장하면 null을 보내 env 값으로 돌린다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(settings(null));
    await input();

    await user.click(screen.getByRole("button", { name: "업로드 최대 크기 재정의 지우기" }));
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: null }));
    await waitFor(() => expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("50MBenv"));
  });

  it("값과 상관없이 앞단 리버스 프록시의 본문 상한을 안내한다 — 빈칸(env)·재정의·하드 상한 근처 모두", async () => {
    const user = userEvent.setup();
    renderSection();
    const field = await input();
    expect(proxyHint()).toHaveTextContent("client_max_body_size");
    expect(proxyHint()).toHaveTextContent("413");

    await user.clear(field);
    await user.type(field, "0.5");
    expect(proxyHint()).not.toBeNull();

    await user.clear(field);
    await user.type(field, "500");
    expect(proxyHint()).not.toBeNull();
    // 지운 GKE ingress(100m) 안내는 남지 않는다
    expect(screen.queryByText(/GKE|proxy-body-size/)).toBeNull();
  });

  it("1MB 미만 값은 소수 MB로 받아 바이트로 반올림해 보낸다(코어 범위 1바이트부터 — D4)", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(settings(512 * 1024));
    const field = await input();

    await user.clear(field);
    await user.type(field, "0.5");
    expect(screen.getByTestId("upload-bytes-preview")).toHaveTextContent("저장하면 524,288바이트");
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: 524_288 }));
    // 1MB 미만 적용 값이 0으로 뭉개지지 않는다 — MB로 나누어떨어지지 않으면 바이트도 함께
    await waitFor(() =>
      expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("0.5MB (524,288바이트)재정의"),
    );
    expect(field).toHaveValue(0.5);
  });

  it("하한 경계 — 1바이트(0.000001MB 반올림)는 보내고, 1바이트보다 작은 값·0은 보내지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(settings(1));
    const field = await input();

    for (const tooSmall of ["0", "0.0000001"]) {
      await user.clear(field);
      await user.type(field, tooSmall);
      expect(screen.queryByTestId("upload-bytes-preview")).toBeNull();
      await user.click(saveButton());
    }
    expect(api.put).not.toHaveBeenCalled();

    await user.clear(field);
    await user.type(field, "0.000001");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: 1 }));
  });

  it("상한 경계 — 500MB(524,288,000바이트)는 보낸다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue(settings(500 * MIB));
    const field = await input();
    await user.clear(field);
    await user.type(field, "500");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_upload_bytes: 500 * MIB }));
  });

  it("1MB 미만이거나 MB로 나누어떨어지지 않는 env·재정의 값도 정확히 보인다", async () => {
    renderSection({
      max_upload_bytes: 1000,
      env_max_upload_bytes: 50 * MIB + 1,
      override: 1000,
      hard_max_upload_bytes: 500 * MIB,
    });
    // 입력은 같은 바이트로 되돌아오는 MB 값(반올림하지 않는다)
    expect(await input()).toHaveValue(1000 / MIB);
    expect(screen.getByTestId("upload-effective-value")).toHaveTextContent("0.000954MB (1,000바이트)재정의");
    expect(screen.getByTestId("upload-env-value")).toHaveTextContent("50MB (52,428,801바이트)");
    expect(saveButton()).toBeDisabled();
  });

  it("하드 상한(500MB)을 넘는 값은 보내지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    const field = await input();
    await user.clear(field);
    await user.type(field, "501");
    await user.click(saveButton());
    expect(api.put).not.toHaveBeenCalled();
  });

  it("422는 서버 상한을 MB로 바꿔 보이고 오류 토스트를 띄우지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockRejectedValue(
      new ApiError(422, "[]", {
        detail: [
          { type: "less_than_equal", loc: ["body", "max_upload_bytes"], msg: "Input should be…", ctx: { le: 200 * MIB } },
        ],
      }),
    );
    const field = await input();
    await user.clear(field);
    await user.type(field, "300");
    await user.click(saveButton());

    expect(await within(section()).findByRole("alert")).toHaveTextContent("업로드 최대 크기: 200MB 이하여야 합니다");
    expect(toast.error).not.toHaveBeenCalled();
  });
});
