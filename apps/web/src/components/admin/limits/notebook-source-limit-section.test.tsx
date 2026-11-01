import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotebookSourceLimitSection } from "@/components/admin/limits/notebook-source-limit-section";
import type { NotebookSourceLimit } from "@/lib/admin-types";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { qk } from "@/lib/query-keys";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const PATH = "/api/v1/admin/notebook-source-limit";

function limit(override: number | null, env = 500): NotebookSourceLimit {
  return { max_sources_per_notebook: override ?? env, env_max_sources_per_notebook: env, override };
}

function renderSection(data: NotebookSourceLimit = limit(null)) {
  api.get.mockImplementation((p: string) =>
    p === PATH ? Promise.resolve(data) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <NotebookSourceLimitSection />
    </QueryClientProvider>,
  );
  return { queryClient };
}

const input = () => screen.findByLabelText("노트북당 소스 수 재정의");
const section = () => screen.getByRole("region", { name: "노트북당 소스 수" });

afterEach(() => vi.resetAllMocks());

describe("NotebookSourceLimitSection — 노트북당 소스 상한(코어 #445, #281)", () => {
  it("env 기본값과 적용 값을 보이고, 재정의가 없으면 env 배지다", async () => {
    renderSection(limit(null));
    expect(await screen.findByTestId("source-limit-env-value")).toHaveTextContent("500개");
    expect(screen.getByTestId("source-limit-effective-value")).toHaveTextContent("500개env");
    expect(screen.queryByTestId("source-limit-warning")).toBeNull();
  });

  it("500을 넘는 값이면 경고만 보이고 저장은 막지 않는다(D4)", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSection(limit(null));
    api.put.mockResolvedValue(limit(800));
    await user.type(await input(), "800");
    expect(screen.getByTestId("source-limit-warning")).toHaveTextContent("500개를 넘는 값입니다");
    await user.click(within(section()).getByRole("button", { name: "소스 수 저장" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_sources_per_notebook: 800 }));
    expect(queryClient.getQueryData(qk.adminCoreLimits.notebookSourceLimit)).toEqual(limit(800));
  });

  it("지우기 → 저장은 null(재정의 삭제)을 보낸다", async () => {
    const user = userEvent.setup();
    renderSection(limit(300));
    api.put.mockResolvedValue(limit(null));
    expect(await input()).toHaveValue(300);
    await user.click(screen.getByRole("button", { name: "노트북당 소스 수 재정의 지우기" }));
    await user.click(within(section()).getByRole("button", { name: "소스 수 저장" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { max_sources_per_notebook: null }));
  });
});
