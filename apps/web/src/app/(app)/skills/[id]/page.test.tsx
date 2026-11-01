/**
 * 스킬 상세 페이지 — 기준선 추적표 #45~#48(계획 AC-21 (a)) + ui-ux-redesign P4(plan §6.6.2 스킬 상세 P-07).
 * 경계(`@/lib/api-client`·라우터·토스트)만 대체한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillDetailPage from "@/app/(app)/skills/[id]/page";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "s1" }),
  useRouter: () => ({ push }),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), getText: vi.fn(), getBlob: vi.fn(), post: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { toast } from "sonner";
import { api, ApiError } from "@/lib/api-client";

const m = vi.mocked(api);

const VERSION = {
  id: "v1",
  version: 1,
  validation_profile: "standard_v1",
  runtime_profile: "standard_agent_v1",
  file_tree: [{ path: "SKILL.md" }, { path: "scripts/fill.py" }],
};

function routes(skill: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  const table: Record<string, unknown> = {
    "/api/v1/skills/s1": skill,
    "/api/v1/skills/s1/versions": [VERSION],
    "/api/v1/skills/s1/review-requests": [],
    "/api/v1/skills/catalog": { enabled: true, scripts_enabled: true, entries: [] },
    ...extra,
  };
  m.get.mockImplementation(async (path: string) => {
    if (!(path in table)) throw new Error(`unexpected GET ${path}`);
    return table[path];
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SkillDetailPage />
    </QueryClientProvider>,
  );
}

const OWNER = {
  id: "s1",
  name: "보고서 스킬",
  description: "d",
  slug: "report",
  visibility: "org",
  origin: "authored",
  can_edit: true,
  can_run: true,
  current_version_id: "v1",
  published_version_id: "v1",
};

afterEach(() => {
  for (const fn of Object.values(m)) (fn as ReturnType<typeof vi.fn>).mockReset();
  push.mockReset();
});

describe("SkillDetailPage (#48)", () => {
  it("비소유자는 공개 version의 SKILL.md를 version 경로로 읽는다(#45 fetchVersionFile 인코딩)", async () => {
    routes({ ...OWNER, can_edit: false, published_version_id: "v 1", current_version_id: "v 1" });
    m.getText.mockResolvedValue("공개 본문");

    renderPage();

    expect(await screen.findByText("공개 본문")).toBeInTheDocument();
    expect(m.getText).toHaveBeenCalledWith("/api/v1/skills/versions/v 1/files/SKILL.md");
    expect(screen.queryByRole("button", { name: /편집/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /등재 요청/ })).not.toBeInTheDocument();
    // 비소유자에게는 검토 요청 목록을 묻지 않는다
    expect(m.get).not.toHaveBeenCalledWith("/api/v1/skills/s1/review-requests");
  });

  it("소유자는 draft SKILL.md를 보고, 카탈로그·스크립트 검토를 요청할 수 있다", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["draft 본문"], { type: "text/plain" }));
    m.post.mockResolvedValue({ id: "r1" });

    renderPage();

    expect(await screen.findByText("draft 본문")).toBeInTheDocument();
    expect(m.getBlob).toHaveBeenCalledWith("/api/v1/skills/s1/files/SKILL.md");
    fireEvent.click(await screen.findByRole("button", { name: /채팅 카탈로그 등재 요청/ }));
    await waitFor(() =>
      expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/versions/v1/review-requests", {
        kind: "expose_org",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /스크립트 실행 허가 요청/ }));
    await waitFor(() =>
      expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/versions/v1/review-requests", {
        kind: "scripts",
      }),
    );
    expect(screen.getByLabelText("v1 내보내기").getAttribute("href")).toContain(
      "/api/v1/skills/versions/v1/export",
    );
  });

  it.each([
    ["pending", "등재 검토 대기", "스크립트 검토 대기"],
    ["approved", "채팅 카탈로그 등재됨", "스크립트 실행 허가됨"],
    ["rejected", "등재 거절됨 — 새 버전으로 다시 요청", "스크립트 거절됨 — 새 버전으로 다시 요청"],
  ])("검토 요청이 %s면 버튼 대신 상태 라벨을 보인다(#46·#47)", async (status, catalogLabel, scriptsLabel) => {
    routes(OWNER, {
      "/api/v1/skills/s1/review-requests": [
        { id: "a", kind: "expose_org", version_id: "v1", status },
        { id: "b", kind: "scripts", version_id: "v1", status },
      ],
    });
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));

    renderPage();

    expect(await screen.findByText(catalogLabel)).toBeInTheDocument();
    expect(screen.getByText(scriptsLabel)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /등재 요청/ })).not.toBeInTheDocument();
  });

  it("런타임이 꺼져 있으면 요청 버튼을 내지 않는다", async () => {
    routes(OWNER, {
      "/api/v1/skills/catalog": { enabled: false, scripts_enabled: false, entries: [] },
    });
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));

    renderPage();

    await screen.findByText("x");
    expect(screen.queryByRole("button", { name: /등재 요청/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /스크립트 실행 허가 요청/ })).not.toBeInTheDocument();
  });

  it("스킬을 찾지 못하면(404) ErrorState와 「스킬 목록으로」 링크만 보인다 — 다시 시도 없음", async () => {
    m.get.mockRejectedValue(new ApiError(404, "not found"));
    renderPage();
    expect(await screen.findByText("스킬을 찾을 수 없습니다")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "스킬 목록으로" })).toHaveAttribute("href", "/skills");
    expect(screen.queryByRole("button", { name: "다시 시도" })).not.toBeInTheDocument();
  });

  it("그 밖의 오류는 「스킬을 불러오지 못했습니다」 + 「다시 시도」(재요청)", async () => {
    m.get.mockRejectedValue(new ApiError(500, "boom"));
    renderPage();
    expect(await screen.findByText("스킬을 불러오지 못했습니다")).toBeInTheDocument();
    const before = m.get.mock.calls.filter(([p]) => p === "/api/v1/skills/s1").length;
    fireEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() =>
      expect(m.get.mock.calls.filter(([p]) => p === "/api/v1/skills/s1").length).toBe(before + 1),
    );
  });
});

describe("P4 — 브레드크럼·실행 안내·배지·주 버튼(P-07)", () => {
  it("브레드크럼 「스킬 / {이름}」(링크는 목록), 제목 h1, 옛 「스킬 목록」 뒤로 링크는 없다", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "보고서 스킬" })).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumbs).getByRole("link", { name: "스킬" })).toHaveAttribute("href", "/skills");
    expect(within(crumbs).getByText("보고서 스킬")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: /스킬 목록/ })).not.toBeInTheDocument();
  });

  it("실행 안내는 실제 위치(에이전트 탭 → 스킬)를 말한다", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    renderPage();
    expect(await screen.findByTestId("skill-run-hint")).toHaveTextContent(
      "실행은 노트북 오른쪽 에이전트 탭 → 스킬에서 합니다.",
    );
  });

  it("상태는 StatusBadge(「조직 공유」·「공개 중」) — 원시값 draft·version 없음, 주(채움) 버튼은 「편집」 하나", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    const { container } = renderPage();
    await screen.findByText("x");
    const meta = screen.getByTestId("skill-meta");
    expect(within(meta).getByText("조직 공유")).toHaveAttribute("data-slot", "status-badge");
    // 「공개 중」은 관리 스킬 공개 화면과 같은 success 톤이다(P6 — 진단 P-07 ③, lib/labels SKILL_STATUS_TONE)
    expect(within(meta).getByText("공개 중")).toHaveAttribute("data-tone", "success");
    expect(container.textContent).not.toMatch(/draft|version/i);
    const actions = container.querySelector<HTMLElement>("[data-slot=page-header-actions]")!;
    const primary = within(actions)
      .getAllByRole("button")
      .filter((b) => b.classList.contains("bg-primary"));
    expect(primary.map((b) => b.textContent?.trim())).toEqual(["편집"]);
  });

  it("버전 목록의 공개 버전 배지도 머리의 「공개 중」과 같은 success 톤이다(P6 — P-07 ③)", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    renderPage();
    await screen.findByText("x");
    await screen.findByText("v1");
    const badges = screen.getAllByText("공개 중");
    expect(badges).toHaveLength(2); // 머리(skill-meta) + 버전 목록 v1
    for (const b of badges) expect(b).toHaveAttribute("data-tone", "success");
  });

  it("미공개 소유 스킬은 「초안」 배지(원시값 draft 아님)", async () => {
    routes({ ...OWNER, published_version_id: null, visibility: "private" });
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    renderPage();
    await screen.findByText("x");
    expect(within(screen.getByTestId("skill-meta")).getByText("초안")).toHaveAttribute("data-tone", "neutral");
  });

  it("검토 요청 성공·실패는 lib/notify 문구로 알린다", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    m.post.mockResolvedValueOnce({ id: "r1" }).mockRejectedValueOnce(new ApiError(409, "dup"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /채팅 카탈로그 등재 요청/ }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("채팅 카탈로그 등재를 요청했습니다", expect.anything()),
    );
    fireEvent.click(screen.getByRole("button", { name: /스크립트 실행 허가 요청/ }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "검토를 요청하지 못했습니다",
        expect.objectContaining({ description: expect.stringContaining("충돌") }),
      ),
    );
  });

  it("「복제」는 clone 1회 뒤 새 스킬 편집 화면으로, 버전 내보내기는 이름 있는 링크(아이콘 버튼)", async () => {
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    m.post.mockResolvedValue({ skill_id: "copy", status: "draft" });
    renderPage();
    await screen.findByText("x");
    const exportLink = screen.getByRole("link", { name: "v1 내보내기" });
    expect(exportLink.getAttribute("href")).toContain("/api/v1/skills/versions/v1/export");
    fireEvent.click(screen.getByRole("button", { name: "복제" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/copy/edit"));
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/clone");
    expect(toast.success).toHaveBeenCalledWith("스킬을 복제했습니다", expect.anything());
  });
});

describe("「다시 시도」 포커스 착지점(P2 회고 HCI-P2-2)", () => {
  it("오류 → 스켈레톤 → 상세로 바뀌는 동안 포커스는 셸 본문(main)에, 결과가 오면 브레드크럼 「스킬」 링크에 있다", async () => {
    const user = userEvent.setup();
    routes(OWNER);
    m.getBlob.mockResolvedValue(new Blob(["x"], { type: "text/plain" }));
    const base = m.get.getMockImplementation()!;
    let skillReply: () => Promise<unknown> = () => Promise.reject(new ApiError(500, "boom"));
    m.get.mockImplementation((path: string) => (path === "/api/v1/skills/s1" ? skillReply() : base(path)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <main id="main-content" tabIndex={-1}>
          <SkillDetailPage />
        </main>
      </QueryClientProvider>,
    );
    await screen.findByText("스킬을 불러오지 못했습니다");
    let release: (value: unknown) => void = () => {};
    skillReply = () => new Promise((resolve) => (release = resolve));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(screen.queryByText("스킬을 불러오지 못했습니다")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(screen.getByRole("main"));
    release(OWNER);
    const crumbs = await screen.findByRole("navigation", { name: "위치" });
    expect(document.activeElement).toBe(within(crumbs).getByRole("link", { name: "스킬" }));
  });
});
