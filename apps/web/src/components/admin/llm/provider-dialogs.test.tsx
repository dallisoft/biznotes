import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAdminProviders } from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";
import { CreateProviderDialog } from "./create-provider-dialog";
import { EditProviderDialog } from "./edit-provider-dialog";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/lib/api-client", () => ({ api }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const PROVIDER: AdminProvider = {
  id: "provider-1",
  name: "emb",
  type: "openai",
  base_url: null,
  has_api_key: true,
  enabled: true,
  model_count: 0,
  embedding_max_concurrency: 4,
};

function CreateHarness() {
  return <CreateProviderDialog create={useAdminProviders().create} />;
}

/** 표 행의 주 동작 「수정」(RowActions)이 제어형 다이얼로그를 여는 배선과 같다. */
function EditHarness({ provider }: { provider: AdminProvider }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        수정
      </button>
      <EditProviderDialog
        provider={provider}
        update={useAdminProviders().update}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue([]);
  api.post.mockResolvedValue(PROVIDER);
  api.patch.mockResolvedValue(PROVIDER);
});

describe("CreateProviderDialog 임베딩 동시 요청 상한", () => {
  async function openAndFill(limit: string) {
    const user = userEvent.setup();
    renderWithClient(<CreateHarness />);
    await user.click(screen.getByRole("button", { name: "프로바이더 추가" }));
    await user.type(screen.getByPlaceholderText("예: bedrock-us"), "emb");
    if (limit) await user.type(screen.getByLabelText(/임베딩 동시 요청 상한/), limit);
    await user.click(screen.getByRole("button", { name: "추가" }));
    return user;
  }

  it("입력한 정수를 embedding_max_concurrency로 보낸다", async () => {
    await openAndFill("3");
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ name: "emb", embedding_max_concurrency: 3 });
  });

  it("비워 두면 null(제한 없음)로 보낸다", async () => {
    await openAndFill("");
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ embedding_max_concurrency: null });
  });

  it("범위 밖 값은 제출하지 않고 안내를 보인다", async () => {
    await openAndFill("65");
    expect(screen.getByText(/1~64 사이의 정수를 입력하거나 비워 두세요/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe("EditProviderDialog 임베딩 동시 요청 상한", () => {
  it("현재 값으로 시드하고, 비우면 null(해제)을 보낸다", async () => {
    const user = userEvent.setup();
    renderWithClient(<EditHarness provider={PROVIDER} />);
    await user.click(screen.getByRole("button", { name: "수정" }));
    const field = screen.getByLabelText(/임베딩 동시 요청 상한/);
    expect(field).toHaveValue("4");

    await user.clear(field);
    await user.click(screen.getByRole("button", { name: "저장" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch.mock.calls[0][1]).toHaveProperty("embedding_max_concurrency", null);
  });

  it("다른 필드만 바꿔도 현재 상한을 그대로 보낸다", async () => {
    const user = userEvent.setup();
    renderWithClient(<EditHarness provider={PROVIDER} />);
    await user.click(screen.getByRole("button", { name: "수정" }));
    await user.click(screen.getByRole("button", { name: "저장" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch.mock.calls[0][1]).toMatchObject({ embedding_max_concurrency: 4 });
  });

  it("닫았다 다시 열면 입력하던 값이 아니라 현재 값으로 다시 시드한다", async () => {
    const user = userEvent.setup();
    renderWithClient(<EditHarness provider={PROVIDER} />);
    await user.click(screen.getByRole("button", { name: "수정" }));
    const field = screen.getByLabelText(/임베딩 동시 요청 상한/);
    await user.clear(field);
    await user.type(field, "9");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "수정" }));
    expect(screen.getByLabelText(/임베딩 동시 요청 상한/)).toHaveValue("4");
    expect(api.patch).not.toHaveBeenCalled();
  });

  it("필드가 없는 구 API 응답이면 빈 칸으로 시드한다", async () => {
    const user = userEvent.setup();
    const legacy = { ...PROVIDER };
    delete legacy.embedding_max_concurrency;
    renderWithClient(<EditHarness provider={legacy} />);
    await user.click(screen.getByRole("button", { name: "수정" }));
    expect(screen.getByLabelText(/임베딩 동시 요청 상한/)).toHaveValue("");
  });
});
