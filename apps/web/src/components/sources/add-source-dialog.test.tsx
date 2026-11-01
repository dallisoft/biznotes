import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AddSourceDialog } from "@/components/sources/add-source-dialog";

const mocks = vi.hoisted(() => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    error: vi.fn(),
  },
  mutateAsync: vi.fn(),
  mutate: vi.fn(),
  // `GET /sources/upload-limits` 응답(코어 v0.26.0 #388) — data가 undefined면 아직 못 읽었거나(조회 중) 실패(isError)
  uploadLimits: {
    data: { max_upload_bytes: 50 * 1024 * 1024, max_sources_per_notebook: 500 } as
      | { max_upload_bytes: number; max_sources_per_notebook?: number }
      | undefined,
    isError: false,
  },
  uploadLimitsEnabled: [] as boolean[],
  // 업로드 직전 한도 확정(조회 중이면 그 요청을 기다린다) — 훅 자체는 `use-upload-limits.test.tsx`가 다룬다
  resolveUploadLimit: vi.fn<() => Promise<number | undefined>>(),
}));

vi.mock("sonner", () => ({ toast: mocks.toast }));
vi.mock("@/hooks/use-sources", () => ({
  useSources: () => ({
    data: [
      { type: "file", title: "old.txt", size_bytes: 3 },
      { type: "web", title: "new.txt", size_bytes: 3 },
    ],
    isPending: false,
  }),
  useCreateSource: () => ({ mutate: mocks.mutate, mutateAsync: mocks.mutateAsync }),
  useUploadSource: () => ({ mutateAsync: mocks.mutateAsync }),
  useUploadLimits: (enabled: boolean) => {
    mocks.uploadLimitsEnabled.push(enabled);
    return mocks.uploadLimits;
  },
  useResolveUploadLimit: () => mocks.resolveUploadLimit,
  useWebSearch: () => ({ mutate: mocks.mutate, isPending: false }),
}));

describe("AddSourceDialog 노트북당 소스 상한 (#281)", () => {
  afterEach(() => {
    mocks.uploadLimits = {
      data: { max_upload_bytes: 50 * 1024 * 1024, max_sources_per_notebook: 500 },
      isError: false,
    };
  });

  it("상한에 닿으면 안내를 보인다", () => {
    render(<AddSourceDialog notebookId="nb1" sourceCount={500} open onOpenChange={vi.fn()} showTrigger={false} />);
    expect(screen.getByText("500/500 사용 중")).toBeInTheDocument();
    expect(screen.getByTestId("sources-full")).toHaveTextContent("노트북당 소스는 최대 500개입니다");
  });

  it("상한을 못 읽으면(옛 api·조회 전) 분모 없이 개수만 보인다", () => {
    mocks.uploadLimits = { data: { max_upload_bytes: 50 * 1024 * 1024 }, isError: false };
    render(<AddSourceDialog notebookId="nb1" sourceCount={1200} open onOpenChange={vi.fn()} showTrigger={false} />);
    expect(screen.getByText("소스 1,200개 사용 중")).toBeInTheDocument();
    expect(screen.queryByTestId("sources-full")).toBeNull();
  });
});

describe("AddSourceDialog file upload UX", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.mutateAsync.mockImplementation(async (file: File) => ({ title: file.name }));
  });

  it("shows the server source limit, filters only file sources, and aggregates upload results", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <AddSourceDialog
        notebookId="nb1"
        sourceCount={7}
        open
        onOpenChange={onOpenChange}
        showTrigger={false}
      />,
    );

    // 노트북당 소스 상한은 `upload-limits`의 `max_sources_per_notebook`(코어 v0.30.0 #445, #281)
    expect(screen.getByText("7/500 사용 중")).toBeInTheDocument();
    expect(screen.queryByTestId("sources-full")).toBeNull();
    const input = document.body.querySelector('input[type="file"]');
    expect(input).not.toBeNull();

    await user.upload(input as HTMLInputElement, [
      new File(["old"], "old.txt"),
      new File(["new"], "new.txt"),
      new File(["new"], "new.txt"),
    ]);

    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.mutateAsync.mock.calls.map(([payload]) => payload.file.name)).toEqual([
      "new.txt",
    ]);
    expect((input as HTMLInputElement).value).toBe("");
    expect(mocks.toast.success).toHaveBeenCalledWith(
      "기존 파일 제외 1개 · 선택 내 중복 제외 1개 · 업로드 성공 1개",
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("웹 검색 「전체 선택」의 일부 선택 표시는 기존 그대로다(CheckIcon, 가로줄 없음 — notebook-bulk-actions P1-01)", async () => {
    mocks.mutate.mockImplementation(
      (_vars: unknown, opts?: { onSuccess?: (data: unknown) => void }) =>
        opts?.onSuccess?.([
          { title: "결과 1", url: "https://example.com/1", snippet: "" },
          { title: "결과 2", url: "https://example.com/2", snippet: "" },
        ]),
    );
    const user = userEvent.setup();
    render(
      <AddSourceDialog
        notebookId="nb1"
        sourceCount={0}
        open
        onOpenChange={vi.fn()}
        showTrigger={false}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "웹 검색" }));
    await user.type(screen.getByPlaceholderText("검색어"), "녹차");
    await user.click(screen.getByRole("button", { name: "검색" }));
    await screen.findByText("결과 1");

    const checkboxes = screen.getAllByRole("checkbox");
    const selectAll = checkboxes[0];
    expect(selectAll.closest("label")).toHaveTextContent("전체 선택");
    await user.click(checkboxes[1]); // 결과 1개만 선택 → 일부 선택
    expect(screen.getByText("1/2개 선택됨")).toBeInTheDocument();
    expect(selectAll).toHaveAttribute("aria-checked", "mixed");
    const icon = selectAll.querySelector('[data-slot="checkbox-indicator"] svg');
    expect(icon).toHaveClass("lucide-check");
    expect(selectAll.querySelector('[data-icon="indeterminate"]')).toBeNull();
    expect(selectAll.className).not.toContain("data-indeterminate:bg-primary");
  });
});

describe("AddSourceDialog — 패널 머리글 트리거·탭 라벨(ui-ux-redesign P3, plan §6.6.2·§6.3.1)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("트리거는 outline + Plus 「소스 추가」(「+ 텍스트」 아님)이고 testid `add-source`를 유지한다", async () => {
    const user = userEvent.setup();
    render(<AddSourceDialog notebookId="nb1" sourceCount={2} />);
    const trigger = screen.getByTestId("add-source");
    expect(trigger).toHaveAccessibleName("소스 추가");
    expect(trigger.textContent).toBe("소스 추가");
    expect(trigger.className).toContain("border-border"); // outline 변형
    expect(trigger.querySelector("svg.lucide-plus")).not.toBeNull();
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "소스 추가" })).toBeInTheDocument();
  });

  it("탭은 파일·웹·웹 검색·텍스트 넷이다(외부 계정 탭 없음)", () => {
    render(
      <AddSourceDialog notebookId="nb1" sourceCount={0} open onOpenChange={vi.fn()} showTrigger={false} />,
    );
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "파일",
      "웹",
      "웹 검색",
      "텍스트",
    ]);
    expect(screen.queryByRole("tab", { name: "연결된 계정" })).toBeNull();
  });
});

describe("AddSourceDialog — 업로드 최대 크기 사전 검사(코어 v0.26.0 #388, #228 AC-13)", () => {
  const MIB = 1024 * 1024;
  /** 내용은 1바이트지만 `size`만 키운 파일 — 큰 버퍼를 만들지 않는다 */
  const sized = (name: string, size: number) => {
    const file = new File(["x"], name);
    Object.defineProperty(file, "size", { value: size });
    return file;
  };
  const renderOpen = (onOpenChange = vi.fn()) =>
    render(
      <AddSourceDialog notebookId="nb1" sourceCount={0} open onOpenChange={onOpenChange} showTrigger={false} />,
    );
  const fileInput = () => document.body.querySelector('input[type="file"]') as HTMLInputElement;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.mutateAsync.mockImplementation(async ({ file }: { file: File }) => ({ title: file.name }));
    mocks.uploadLimits = { data: { max_upload_bytes: 10 * MIB }, isError: false };
    mocks.uploadLimitsEnabled = [];
    mocks.resolveUploadLimit.mockReset();
  });
  afterEach(() => {
    mocks.uploadLimits = {
      data: { max_upload_bytes: 50 * MIB, max_sources_per_notebook: 500 },
      isError: false,
    };
  });

  it("문구에 서버가 준 최대 크기를 보이고, 한도는 다이얼로그가 열려 있을 때만 읽는다", () => {
    const { unmount } = renderOpen();
    expect(screen.getByTestId("upload-hint")).toHaveTextContent(
      "PDF · DOCX · PPTX · XLSX · TXT · MD (최대 10MB)",
    );
    expect(screen.queryByText(/최대 50MB/)).toBeNull();
    expect(mocks.uploadLimitsEnabled).not.toContain(false);
    unmount();

    mocks.uploadLimitsEnabled = [];
    render(<AddSourceDialog notebookId="nb1" sourceCount={0} />);
    expect(mocks.uploadLimitsEnabled.length).toBeGreaterThan(0);
    expect(mocks.uploadLimitsEnabled).not.toContain(true);
  });

  it("한도를 넘는 파일은 보내지 않고, 결과 안내에 그 이름과 한도를 보인다", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderOpen(onOpenChange);

    await user.upload(fileInput(), [
      sized("small.txt", 3),
      sized("big.pdf", 10 * MIB + 1),
      sized("exact.pdf", 10 * MIB),
      sized("huge.docx", 200 * MIB),
    ]);

    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.mutateAsync.mock.calls.map(([payload]) => payload.file.name).sort()).toEqual([
      "exact.pdf",
      "small.txt",
    ]);
    await waitFor(() =>
      expect(mocks.toast.warning).toHaveBeenCalledWith("크기 초과 제외 2개 · 업로드 성공 2개", {
        description: "최대 10MB보다 커서 올리지 않았습니다: big.pdf, huge.docx",
      }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("모두 한도를 넘으면 아무것도 보내지 않고 다이얼로그를 닫지 않는다", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderOpen(onOpenChange);

    await user.upload(fileInput(), [sized("big.pdf", 11 * MIB)]);

    await waitFor(() =>
      expect(mocks.toast.warning).toHaveBeenCalledWith("크기 초과 제외 1개", {
        description: "최대 10MB보다 커서 올리지 않았습니다: big.pdf",
      }),
    );
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("넘는 파일이 많으면 이름은 다섯 개까지 보이고 나머지는 개수로 줄인다", async () => {
    const user = userEvent.setup();
    renderOpen();

    await user.upload(
      fileInput(),
      Array.from({ length: 7 }, (_, i) => sized(`big-${i + 1}.pdf`, 11 * MIB)),
    );

    await waitFor(() =>
      expect(mocks.toast.warning).toHaveBeenCalledWith("크기 초과 제외 7개", {
        description: "최대 10MB보다 커서 올리지 않았습니다: big-1.pdf, big-2.pdf, big-3.pdf, big-4.pdf, big-5.pdf 외 2개",
      }),
    );
  });

  it("한도 조회에 실패했으면 기다리지 않고 사전 검사 없이 보내며, 서버 413을 「크기 초과(413)」로 센다", async () => {
    mocks.uploadLimits = { data: undefined, isError: true };
    mocks.mutateAsync.mockRejectedValue({ status: 413 });
    const user = userEvent.setup();
    renderOpen();
    expect(screen.getByTestId("upload-hint")).toHaveTextContent(/MD$/);
    expect(screen.getByTestId("upload-hint")).not.toHaveTextContent("최대");

    await user.upload(fileInput(), [sized("big.pdf", 600 * MIB)]);

    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.resolveUploadLimit).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.toast.warning).toHaveBeenCalledWith("크기 초과(413) 1개"));
  });

  it("처음 조회가 끝나기 전에 고른 파일은 한도가 올 때까지 보내지 않고, 한도가 오면 넘는 파일만 빼고 보낸다", async () => {
    mocks.uploadLimits = { data: undefined, isError: false };
    let resolve!: (maxBytes: number | undefined) => void;
    mocks.resolveUploadLimit.mockImplementation(() => new Promise((r) => (resolve = r)));
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderOpen(onOpenChange);

    await user.upload(fileInput(), [sized("small.txt", 3), sized("big.pdf", 11 * MIB)]);

    expect(await screen.findByText("업로드 한도를 확인하는 중입니다…")).toBeInTheDocument();
    expect(mocks.resolveUploadLimit).toHaveBeenCalledTimes(1);
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
    expect(fileInput()).toBeDisabled();

    await act(async () => resolve(10 * MIB));

    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.mutateAsync.mock.calls.map(([payload]) => payload.file.name)).toEqual(["small.txt"]);
    await waitFor(() =>
      expect(mocks.toast.warning).toHaveBeenCalledWith("크기 초과 제외 1개 · 업로드 성공 1개", {
        description: "최대 10MB보다 커서 올리지 않았습니다: big.pdf",
      }),
    );
    expect(screen.queryByText("업로드 한도를 확인하는 중입니다…")).toBeNull();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("한도를 기다리던 중 조회가 실패하면(undefined) 사전 검사 없이 보낸다", async () => {
    mocks.uploadLimits = { data: undefined, isError: false };
    let resolve!: (maxBytes: number | undefined) => void;
    mocks.resolveUploadLimit.mockImplementation(() => new Promise((r) => (resolve = r)));
    const user = userEvent.setup();
    renderOpen();

    await user.upload(fileInput(), [sized("big.pdf", 600 * MIB)]);
    await screen.findByText("업로드 한도를 확인하는 중입니다…");
    expect(mocks.mutateAsync).not.toHaveBeenCalled();

    await act(async () => resolve(undefined));
    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.mutateAsync.mock.calls[0][0].file.name).toBe("big.pdf");
  });

  it("한도를 기다리는 동안 다이얼로그를 닫으면 한도가 와도 보내지 않는다", async () => {
    mocks.uploadLimits = { data: undefined, isError: false };
    let resolve!: (maxBytes: number | undefined) => void;
    mocks.resolveUploadLimit.mockImplementation(() => new Promise((r) => (resolve = r)));
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderOpen(onOpenChange);

    await user.upload(fileInput(), [sized("small.txt", 3)]);
    await screen.findByText("업로드 한도를 확인하는 중입니다…");
    await user.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);

    await act(async () => resolve(10 * MIB));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
    expect(mocks.toast.warning).not.toHaveBeenCalled();
    expect(mocks.toast.success).not.toHaveBeenCalled();
  });
});
