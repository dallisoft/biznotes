/**
 * 스킬 편집 페이지 — 기준선 #39~#44 + 계획 P0-16(kind별 검토 배너)·P0-18(공유 이중 제출 방지)
 * + ui-ux-redesign P4(AC-4.4·4.5 — 권한 없음 ErrorState, 뒤로 링크 이름, 「초안」 배지).
 * 경계(`@/lib/api-client`·라우터·토스트·window.confirm)만 대체한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillEditPage from "@/app/(app)/skills/[id]/edit/page";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "s1" }), useRouter: () => ({ push }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/api-client", () => ({
  api: {
    get: vi.fn(),
    getBlob: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    putBytes: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
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

const SKILL = {
  id: "s1",
  name: "보고서",
  description: "목록 설명",
  origin: "authored",
  can_edit: true,
  current_version_id: "v1",
  published_version_id: null,
};
const FRONTMATTER = {
  editable: true,
  fields: { name: "report", description: "보고서를 쓴다" },
  source_hash: "hash-1",
};

function routes(extra: Record<string, unknown> = {}) {
  const table: Record<string, unknown> = {
    "/api/v1/skills/s1": SKILL,
    "/api/v1/skills/s1/files": [
      { path: "SKILL.md", role: "skill_md", size: 10, media_type: "text/markdown" },
      { path: "assets/logo.png", role: "asset", size: 3, media_type: "image/png" },
    ],
    "/api/v1/skills/s1/frontmatter": FRONTMATTER,
    "/api/v1/skills/s1/review-requests": [],
    ...extra,
  };
  m.get.mockImplementation(async (path: string) => {
    if (!(path in table)) throw new Error(`unexpected GET ${path}`);
    return table[path];
  });
  m.getBlob.mockResolvedValue(new Blob(["---\nname: report\n---\n본문"], { type: "text/plain" }));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SkillEditPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  for (const fn of Object.values(m)) (fn as ReturnType<typeof vi.fn>).mockReset();
  for (const fn of Object.values(toast)) (fn as ReturnType<typeof vi.fn>).mockReset();
  push.mockReset();
  vi.restoreAllMocks();
});

describe("frontmatter 폼 (#39·#41·#42)", () => {
  it("FORM_FIELDS 네 칸을 그리고, 수정 → 저장이 source_hash와 함께 PUT된다", async () => {
    routes();
    m.post.mockResolvedValue({ ok: true });
    m.put.mockResolvedValue({ fields: { name: "report-2" }, source_hash: "hash-2", editable: true });
    renderPage();

    for (const field of ["name", "description", "license", "compatibility"]) {
      expect(await screen.findByLabelText(field)).toBeInTheDocument();
    }
    const nameInput = screen.getByLabelText("name");
    expect(nameInput).toHaveValue("report");
    fireEvent.change(nameInput, { target: { value: "report-2" } });
    // 디바운스(350ms) 검증이 같은 페이로드로 먼저 나간다(useValidateSkillFrontmatter)
    await waitFor(() =>
      expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/frontmatter/validate", {
        fields: { name: "report-2", description: "보고서를 쓴다" },
        source_hash: "hash-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /frontmatter 저장/ }));

    await waitFor(() =>
      expect(m.put).toHaveBeenCalledWith("/api/v1/skills/s1/frontmatter", {
        fields: { name: "report-2", description: "보고서를 쓴다" },
        source_hash: "hash-1",
      }),
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("frontmatter 저장됨"));
  });

  it("name을 비우면 오류를 보이고 저장할 수 없다", async () => {
    routes();
    renderPage();
    fireEvent.change(await screen.findByLabelText("name"), { target: { value: "  " } });
    expect(screen.getByText(/name은 비어 있을 수 없습니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /frontmatter 저장/ })).toBeDisabled();
  });

  it("SKILL.md 원문에 저장하지 않은 변경이 있으면 폼 저장을 막는다", async () => {
    routes();
    renderPage();
    const editor = await screen.findByDisplayValue(/본문/);
    fireEvent.change(editor, { target: { value: "바뀐 원문" } });
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "other" } });
    fireEvent.click(screen.getByRole("button", { name: /frontmatter 저장/ }));
    expect(toast.error).toHaveBeenCalledWith(
      "저장하지 않은 SKILL.md 원문을 먼저 저장하거나 되돌리세요.",
    );
    expect(m.put).not.toHaveBeenCalled();
  });

  it("편집할 수 없는 frontmatter는 이유를 보이고 폼을 내지 않는다", async () => {
    routes({
      "/api/v1/skills/s1/frontmatter": {
        editable: false,
        reason: "frontmatter_comments",
        source_hash: "h",
      },
    });
    renderPage();
    expect(await screen.findByText(/주석이/)).toBeInTheDocument();
    expect(screen.queryByLabelText("name")).not.toBeInTheDocument();
  });
});

describe("동결 → 공유 (#43, P0-18)", () => {
  it("조직에 공유는 동결 뒤 version을 공개하고, 202면 검토 대기를 알린다", async () => {
    routes();
    m.post.mockImplementation(async (path: string) =>
      path.endsWith("/freeze")
        ? { version_id: "v2", version: 2, wrapper_agent_id: "w", status: "frozen" }
        : { status: "review_pending", version_id: "v2", request_id: "r1" },
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "조직에 공유" }));

    await waitFor(() =>
      expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/versions/v2/publish"),
    );
    expect(m.post.mock.calls[0]).toEqual(["/api/v1/skills/s1/freeze", {}]);
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith("관리자 검토 대기 — 승인되면 조직에 공개됩니다"),
    );
  });

  it("공개가 바로 되면 성공을 알린다(200)", async () => {
    routes();
    m.post.mockImplementation(async (path: string) =>
      path.endsWith("/freeze")
        ? { version_id: "v2", version: 2, wrapper_agent_id: "w", status: "frozen" }
        : { status: "published", version_id: "v2", request_id: null },
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "조직에 공유" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("v2 공개(org)했습니다"));
  });

  it("공개 slug가 겹치면(409 slug_conflict) 코어 안내 문구를 그대로 보인다(코어 v0.28.0 #412)", async () => {
    routes();
    const conflict = "같은 slug의 공개 스킬이 이미 있습니다 — 복제해 다른 이름으로 공개하세요";
    m.post.mockImplementation(async (path: string) => {
      if (path.endsWith("/freeze")) return { version_id: "v2", version: 2, wrapper_agent_id: "w", status: "frozen" };
      throw new ApiError(409, conflict);
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "조직에 공유" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(`공개 실패: ${conflict}`));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("동결이 진행 중이면 조직에 공유를 다시 누를 수 없다(이중 동결 방지)", async () => {
    routes();
    let finish: (value: unknown) => void = () => {};
    m.post.mockImplementation(
      (path: string) =>
        new Promise((resolve) => {
          if (path.endsWith("/freeze")) finish = resolve;
          else resolve({ status: "published", version_id: "v2", request_id: null });
        }),
    );
    renderPage();
    const share = await screen.findByRole("button", { name: "조직에 공유" });

    fireEvent.click(share);
    await waitFor(() => expect(share).toBeDisabled());
    fireEvent.click(share);
    expect(m.post.mock.calls.filter(([p]) => String(p).endsWith("/freeze"))).toHaveLength(1);

    await act(async () =>
      finish({ version_id: "v2", version: 2, wrapper_agent_id: "w", status: "frozen" }),
    );
    await waitFor(() => expect(share).toBeEnabled());
  });

  it("동결만 하면 공개 요청을 보내지 않는다", async () => {
    routes();
    m.post.mockResolvedValue({ version_id: "v2", version: 2, wrapper_agent_id: "w", status: "frozen" });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "동결" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("v2 동결됨"));
    expect(m.post).toHaveBeenCalledTimes(1);
  });
});

describe("바이너리 업로드 (#44)", () => {
  function upload(name: string) {
    const input = document.getElementById("skill-binary-upload") as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], name, { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });
    return file;
  }

  it("assets/ 아래로 원본 바이트를 올린다", async () => {
    routes();
    m.putBytes.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("assets/logo.png");
    const file = upload("chart.png");
    await waitFor(() =>
      expect(m.putBytes).toHaveBeenCalledWith("/api/v1/skills/s1/files/assets/chart.png", file),
    );
  });

  it("같은 경로가 있으면 확인을 묻고, 거절하면 올리지 않는다", async () => {
    routes();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByText("assets/logo.png");
    upload("logo.png");
    expect(confirm).toHaveBeenCalledWith("assets/logo.png 파일이 이미 있습니다. 덮어쓸까요?");
    expect(m.putBytes).not.toHaveBeenCalled();
  });
});

describe("검토 대기 배너 (#40, P0-16)", () => {
  it.each([
    ["scripts", "authored", "번들 스크립트를 실행할 수 있습니다"],
    ["expose_org", "authored", "채팅 카탈로그에 등재됩니다"],
    ["expose_org", "imported", "승인되면 조직에 공개됩니다."],
  ])("%s(%s) 요청은 승인의 실제 효과를 말한다", async (kind, origin, text) => {
    routes({
      "/api/v1/skills/s1": { ...SKILL, origin },
      "/api/v1/skills/s1/review-requests": [{ id: "r", kind, status: "pending", version_id: "v1" }],
    });
    renderPage();
    expect(await screen.findByText(/관리자 검토 대기 중입니다/)).toHaveTextContent(text);
  });

  it("편집 권한이 없으면 편집기를 내지 않는다", async () => {
    routes({ "/api/v1/skills/s1": { ...SKILL, can_edit: false } });
    renderPage();
    expect(await screen.findByText("이 스킬을 편집할 권한이 없습니다")).toBeInTheDocument();
    expect(screen.queryByLabelText("표시 이름")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "조직에 공유" })).not.toBeInTheDocument();
  });
});

describe("권한 없음·오류 화면(AC-4.5, §8.3 판정 무변경)", () => {
  it("권한 없음은 ErrorState — 「복제해서 내 스킬로 편집」과 「스킬 상세로」가 있고, 쓰기 요청은 0건", async () => {
    routes({ "/api/v1/skills/s1": { ...SKILL, can_edit: false } });
    renderPage();
    const box = (await screen.findByText("이 스킬을 편집할 권한이 없습니다")).closest<HTMLElement>(
      "[data-slot=error-state]",
    )!;
    expect(box).toHaveAttribute("data-variant", "forbidden");
    expect(within(box).getByRole("button", { name: "복제해서 내 스킬로 편집" })).toBeInTheDocument();
    expect(within(box).getByRole("link", { name: "스킬 상세로" })).toHaveAttribute("href", "/skills/s1");
    for (const fn of [m.post, m.put, m.putBytes, m.patch, m.delete]) expect(fn).not.toHaveBeenCalled();
  });

  it("「복제해서 내 스킬로 편집」은 clone 1회 뒤 복제본 편집 화면으로 간다", async () => {
    routes({ "/api/v1/skills/s1": { ...SKILL, can_edit: false } });
    m.post.mockResolvedValue({ skill_id: "copy-9", status: "draft" });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "복제해서 내 스킬로 편집" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/copy-9/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/clone");
  });

  it("복제가 거부되면(403) 실패 토스트, 이동 없음", async () => {
    routes({ "/api/v1/skills/s1": { ...SKILL, can_edit: false } });
    m.post.mockRejectedValue(new ApiError(403, "no"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "복제해서 내 스킬로 편집" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "스킬을 복제하지 못했습니다",
        expect.objectContaining({ description: "이 작업을 할 권한이 없습니다." }),
      ),
    );
    expect(push).not.toHaveBeenCalled();
  });

  it("스킬이 없으면(404) 권한 없음이 아니라 「스킬을 찾을 수 없습니다」 + 목록 링크", async () => {
    m.get.mockRejectedValue(new ApiError(404, "not found"));
    renderPage();
    expect(await screen.findByText("스킬을 찾을 수 없습니다")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "스킬 목록으로" })).toHaveAttribute("href", "/skills");
    expect(screen.queryByText("이 스킬을 편집할 권한이 없습니다")).not.toBeInTheDocument();
  });
});

describe("편집기 머리(AC-4.4·4.5)", () => {
  it("뒤로 링크의 접근 가능한 이름은 「스킬 상세로」, 상태 배지는 「초안」, 원시값 draft 없음, 주 버튼은 「조직에 공유」 하나", async () => {
    routes();
    const { container } = renderPage();
    const back = await screen.findByRole("link", { name: "스킬 상세로" });
    expect(back).toHaveAttribute("href", "/skills/s1");
    const header = back.closest("header")!;
    expect(within(header).getByText("초안")).toHaveAttribute("data-slot", "status-badge");
    expect(container.textContent).not.toMatch(/\bdraft\b/);
    const primary = within(header)
      .getAllByRole("button")
      .filter((b) => b.classList.contains("bg-primary"));
    expect(primary.map((b) => b.textContent)).toEqual(["조직에 공유"]);
  });
});

describe("「다시 시도」 포커스 착지점(P2 회고 HCI-P2-2)", () => {
  it("오류 → 스켈레톤 → 편집기로 바뀌는 동안 포커스는 셸 본문(main)에, 결과가 오면 뒤로 링크 「스킬 상세로」에 있다", async () => {
    const user = userEvent.setup();
    routes();
    const base = m.get.getMockImplementation()!;
    let skillReply: () => Promise<unknown> = () => Promise.reject(new ApiError(500, "boom"));
    m.get.mockImplementation((path: string) => (path === "/api/v1/skills/s1" ? skillReply() : base(path)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <main id="main-content" tabIndex={-1}>
          <SkillEditPage />
        </main>
      </QueryClientProvider>,
    );
    await screen.findByText("스킬을 불러오지 못했습니다");
    let release: (value: unknown) => void = () => {};
    skillReply = () => new Promise((resolve) => (release = resolve));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(screen.queryByText("스킬을 불러오지 못했습니다")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(screen.getByRole("main"));
    release(SKILL);
    const back = await screen.findByRole("link", { name: "스킬 상세로" });
    expect(document.activeElement).toBe(back);
  });
});
