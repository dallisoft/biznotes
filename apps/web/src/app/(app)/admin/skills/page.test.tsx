/**
 * 관리자 스킬 공개 페이지 — 기준선 #38 + 계획 P0-03(검토 상세 열람 뒤 승인)·P0-17(철회·거절 확인)
 * + ui-ux-redesign P5(AC-5.1·5.3·5.6, D-10 공개 철회 확인, P-21).
 * 네트워크 경계(`api` 객체)와 토스트(sonner)만 대체하고 페이지·훅·검토 패널·RowActions·ConfirmDialog(포커스 코드 포함)는
 * 실제로 실행한다(§3.3-3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdminSkillsPage from "@/app/(app)/admin/skills/page";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";

const { toast } = vi.hoisted(() => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("sonner", () => ({ toast }));
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), getText: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const REQUEST = {
  id: "r1",
  version_id: "v1",
  kind: "scripts",
  status: "pending",
  skill_name: "양식 채우기",
  version: 3,
  note: null,
};
const SKILL = {
  id: "s1",
  name: "양식 채우기",
  slug: "form-filler",
  visibility: "org",
  published_version_id: "v1",
  exposure_hold: false,
  pending_review_count: 1,
  run_count: 2,
  clone_count: 0,
  enabled: true,
};
const DRAFT = {
  ...SKILL,
  id: "s2",
  name: "초안 스킬",
  slug: "draft-skill",
  visibility: "private",
  published_version_id: null,
  pending_review_count: 0,
};
const HELD = { ...DRAFT, id: "s3", name: "보류 스킬", slug: "held-skill", exposure_hold: true };

/** 서버 상태 — GET은 이 값을 돌려주고, 쓰기 응답이 이 값을 바꿀 수 있다(무효화 뒤 다시 읽기 경로 확인). */
const server: { skills: unknown[]; reviews: unknown[]; skillsError: boolean; reviewsError: boolean } = {
  skills: [],
  reviews: [],
  skillsError: false,
  reviewsError: false,
};

function renderPage({
  skills = [SKILL],
  reviews = [REQUEST],
  skillsError = false,
  reviewsError = false,
}: { skills?: unknown[]; reviews?: unknown[]; skillsError?: boolean; reviewsError?: boolean } = {}) {
  Object.assign(server, { skills, reviews, skillsError, reviewsError });
  api.get.mockImplementation(async (path: string) => {
    if (path === "/api/v1/admin/skills") {
      if (server.skillsError) throw new Error("boom");
      return server.skills;
    }
    if (path.startsWith("/api/v1/admin/skills/review-requests")) {
      if (server.reviewsError) throw new Error("boom");
      return server.reviews;
    }
    if (path === "/api/v1/admin/skills/versions/v1/files") {
      return [
        { path: "SKILL.md", role: "skill_md", size: 1, media_type: "text/markdown", hash: "a" },
        { path: "scripts/fill.py", role: "script", size: 1, media_type: "text/x-python", hash: "b" },
      ];
    }
    throw new Error(`unexpected GET ${path}`);
  });
  api.getText.mockImplementation(async (path: string) =>
    path.endsWith("fill.py") ? "import os" : "---\nname: form-filler\n---",
  );
  api.post.mockResolvedValue({});
  const client = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <main id="main-content" tabIndex={-1}>
        <AdminSkillsPage />
      </main>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
});

const more = (name: string) => screen.getByRole("button", { name: `${name} 동작 더보기` });

describe("AdminSkillsPage 검토 요청 (#38, P0-03)", () => {
  it("승인은 검토 상세에서 필수 파일(스크립트 원문)을 모두 연 뒤에만 켜진다", async () => {
    renderPage();
    const approve = await screen.findByRole("button", { name: "승인" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("내용을 확인한 뒤 승인할 수 있습니다")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "내용 보기" }));
    expect(await screen.findByTestId("review-version-panel")).toBeInTheDocument();
    await screen.findByText(/name: form-filler/);
    expect(approve).toBeDisabled(); // 스크립트 허가 요청 — SKILL.md만으로는 부족하다

    fireEvent.click(screen.getByRole("button", { name: /scripts\/fill\.py/ }));
    await screen.findByText("import os");
    await waitFor(() => expect(approve).toBeEnabled());
    expect(screen.queryByText("내용을 확인한 뒤 승인할 수 있습니다")).not.toBeInTheDocument();

    fireEvent.click(approve);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/skills/review-requests/r1/approve"),
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("검토 요청을 승인했습니다", expect.anything()));
  });

  it("거절은 사유 입력 대화를 거쳐 note와 함께 보낸다(P0-17)", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "거절" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.post).not.toHaveBeenCalled(); // 1클릭으로 거절되지 않는다
    expect(within(dialog).getByRole("heading", { name: "검토 요청을 거절할까요?" })).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText("거절 사유(선택)"), {
      target: { value: "  외부 전송 코드  " },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "거절" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/skills/review-requests/r1/reject", {
        note: "외부 전송 코드",
      }),
    );
  });

  it("거절 대화를 취소하면 아무것도 보내지 않는다", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "거절" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.post).not.toHaveBeenCalled();
  });

  it("대기 중인 요청이 없으면 표 대신 EmptyState", async () => {
    renderPage({ reviews: [] });
    expect(await screen.findByText("대기 중인 검토 요청이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "승인" })).not.toBeInTheDocument();
  });

  it("검토 요청을 못 불러오면 ErrorState(「다시 시도」)", async () => {
    renderPage({ reviewsError: true });
    expect(await screen.findByText("검토 요청을 불러오지 못했습니다")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "다시 시도" }).length).toBeGreaterThan(0);
  });
});

describe("AdminSkillsPage 헤더·섹션 (AC-5.1, P-21 ①)", () => {
  it("PageHeader: 브레드크럼 「관리 / 스킬 공개」, h1 하나, 섹션 제목은 h2 text-lg", async () => {
    renderPage();
    await screen.findByText("form-filler");
    const crumb = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumb).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(crumb).getByText("스킬 공개")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["스킬 공개"]);

    const h2 = screen.getAllByRole("heading", { level: 2 });
    expect(h2.map((h) => h.textContent)).toEqual(["검토 요청", "스킬 목록"]);
    for (const h of h2) expect(h).toHaveClass("text-lg");
    // e2e의 exact heading 「스킬」과 겹치지 않는다(§9 공통 규칙)
    expect(screen.queryByRole("heading", { name: "스킬" })).not.toBeInTheDocument();
    // 「공개 version」 혼용 없음
    expect(screen.queryByText(/version/)).not.toBeInTheDocument();
  });
});

describe("AdminSkillsPage 스킬 목록 (#38, P0-17, AC-5.3, AC-5.6, D-10)", () => {
  it("공개 상태는 글자 있는 StatusBadge — 공개 중 · 검토 대기 n건 · 초안 · 공개 철회됨", async () => {
    renderPage({ skills: [SKILL, DRAFT, HELD] });
    const published = await screen.findByText("공개 중");
    expect(published).toHaveAttribute("data-slot", "status-badge");
    expect(published).toHaveAttribute("data-tone", "success");
    expect(screen.getByText("검토 대기 1건")).toHaveAttribute("data-tone", "info");
    expect(screen.getByText("초안")).toHaveAttribute("data-tone", "neutral");
    expect(screen.getByText("공개 철회됨")).toHaveAttribute("data-tone", "warning");
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(
      expect.arrayContaining(["이름", "공개 범위", "공개 상태", "실행", "복제", "활성", "동작"]),
    );
  });

  it("공개 범위 system은 「기본 제공」, 검토 종류 expose_system은 「기본 제공 승격」으로 보인다 — 「빌트인」 0(§6.3.1, P5 리뷰 P5-06)", async () => {
    const SYSTEM = { ...SKILL, id: "s9", name: "보고서 생성", slug: "report-writer", visibility: "system" };
    const PROMOTE = { ...REQUEST, id: "r9", kind: "expose_system", skill_name: "보고서 생성" };
    const { container } = renderPage({ skills: [SYSTEM], reviews: [PROMOTE] });
    const row = (await screen.findByText("report-writer")).closest("tr")!;
    expect(within(row).getByText("기본 제공")).toBeInTheDocument();
    const queue = screen.getByRole("heading", { level: 2, name: "검토 요청" }).closest("section")!;
    expect(await within(queue).findByText("기본 제공 승격")).toBeInTheDocument();
    expect(container.textContent).not.toContain("빌트인");
  });

  it("「공개 철회」는 더보기의 위험 항목이고 확인 전에는 요청 0회, 확인 후 1회", async () => {
    const user = userEvent.setup();
    renderPage();
    // 배지 옆 맨 텍스트 버튼은 없다(P-21 ③)
    await screen.findByText("공개 중");
    expect(screen.queryByRole("button", { name: /철회/ })).not.toBeInTheDocument();

    await user.click(more("양식 채우기"));
    const item = await screen.findByRole("menuitem", { name: "공개 철회" });
    await user.click(item);
    expect(api.post).not.toHaveBeenCalled();

    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent("‘양식 채우기’ 공개를 철회할까요?");
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "취소" })).toHaveFocus());
    await user.click(within(confirm).getByRole("button", { name: "공개 철회" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/skills/s1/published-version", {
        version_id: null,
      }),
    );
    expect(api.post).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("‘양식 채우기’ 스킬 공개를 철회했습니다", expect.anything()),
    );
  });

  it("공개 철회를 취소하면 요청 0회이고 포커스는 그 행의 ⋯로 돌아간다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("공개 중");
    await user.click(more("양식 채우기"));
    await user.click(await screen.findByRole("menuitem", { name: "공개 철회" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(more("양식 채우기")).toHaveFocus());
    expect(api.post).not.toHaveBeenCalled();
  });

  it("철회 뒤 다시 읽은 행에 ⋯가 사라지면 포커스는 「스킬 목록」 제목으로 옮겨진다(계약 ③)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("공개 중");
    // 서버: 철회되면 공개 포인터가 비고 보류가 선다
    api.post.mockImplementation(async () => {
      server.skills = [{ ...SKILL, published_version_id: null, exposure_hold: true }];
      return {};
    });
    await user.click(more("양식 채우기"));
    await user.click(await screen.findByRole("menuitem", { name: "공개 철회" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "공개 철회" }));

    expect(await screen.findByText("공개 철회됨")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "양식 채우기 동작 더보기" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "스킬 목록" })).toHaveFocus());
  });

  it("공개되지 않은 스킬에는 공개 철회 메뉴(⋯)가 없다", async () => {
    renderPage({ skills: [DRAFT] });
    await screen.findByText("초안");
    expect(screen.queryByRole("button", { name: "초안 스킬 동작 더보기" })).not.toBeInTheDocument();
  });

  it("활성 스위치는 「{이름} 활성」 이름을 갖고 enabled를 PATCH한다", async () => {
    const user = userEvent.setup();
    api.patch.mockResolvedValue({});
    renderPage();
    const sw = await screen.findByRole("switch", { name: "양식 채우기 활성" });
    await user.click(sw);
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/api/v1/admin/skills/s1", { enabled: false }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("‘양식 채우기’ 스킬을 비활성화했습니다", expect.anything()),
    );
  });

  it("활성 전환 실패는 토스트 한 번(「…하지 못했습니다」)", async () => {
    const user = userEvent.setup();
    api.patch.mockRejectedValue(new Error("boom"));
    renderPage();
    await user.click(await screen.findByRole("switch", { name: "양식 채우기 활성" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error.mock.calls[0][0]).toBe("‘양식 채우기’ 스킬을 비활성화하지 못했습니다");
  });

  it("스킬이 없으면 표·페이지네이션 대신 EmptyState", async () => {
    renderPage({ skills: [], reviews: [] });
    expect(await screen.findByText("아직 스킬이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("페이지당 개수")).not.toBeInTheDocument();
  });

  it("스킬 목록을 못 불러오면 ErrorState, 「다시 시도」로 다시 읽는다", async () => {
    const user = userEvent.setup();
    renderPage({ skillsError: true, reviews: [] });
    expect(await screen.findByText("스킬 목록을 불러오지 못했습니다")).toBeInTheDocument();
    server.skillsError = false;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByText("form-filler")).toBeInTheDocument();
    // 성공하면 포커스는 다시 그려진 스킬 표 안(첫 행 스위치)으로 간다(HCI-P2-2)
    await waitFor(() => expect(screen.getByRole("switch", { name: "양식 채우기 활성" })).toHaveFocus());
  });

  it("키보드로 ⋯를 열고 「공개 철회」를 고를 수 있다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("공개 중");
    act(() => more("양식 채우기").focus());
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    // 메뉴의 첫 포커스는 다음 프레임이다 — 항목에 포커스가 생긴 뒤 이동한다(P6, 부하에서 목표를 지나치는 경합)
    await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
    for (let i = 0; i < 3 && document.activeElement?.textContent !== "공개 철회"; i += 1) {
      await user.keyboard("{ArrowDown}");
    }
    expect(document.activeElement).toBe(within(menu).getByRole("menuitem", { name: "공개 철회" }));
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe("AdminSkillsPage 좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04)", () => {
  it("검토 요청·스킬 목록 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    renderPage();
    await screen.findByText("form-filler");
    const tables = await screen.findAllByRole("table");
    expect(tables).toHaveLength(2);
    tables.forEach((t) => expect(t.closest('[data-slot="admin-table-frame"]')).not.toBeNull());
  });
});

describe("AdminSkillsPage 검토 요청 — 좁은 화면 행 동작 접기 (plan §6.7, P5 재리뷰 P5-R01)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const reviewMore = () => screen.getByRole("button", { name: "‘양식 채우기’ 검토 요청 동작 더보기" });

  async function openReviewMenu(user: ReturnType<typeof userEvent.setup>) {
    await user.click(reviewMore());
    return screen.findByRole("menu");
  }

  it("< 768: 처리 칸은 ⋯ 하나 — 메뉴는 「내용 보기 · 승인 · 거절」이고 승인은 사유와 함께 비활성(요청 0회)", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    renderPage();
    const queue = screen.getByRole("heading", { level: 2, name: "검토 요청" }).closest("section")!;
    await within(queue).findByText("양식 채우기");
    const row = within(queue).getByText("양식 채우기").closest("tr")!;
    await waitFor(() => expect(within(row).queryByRole("button", { name: "승인" })).toBeNull());
    const actions = row.lastElementChild as HTMLElement;
    expect(within(actions).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "‘양식 채우기’ 검토 요청 동작 더보기",
    ]);
    // 넓은 화면의 사유 문단 대신 메뉴 항목 아래에 사유를 보인다
    expect(within(row).queryByText("내용을 확인한 뒤 승인할 수 있습니다")).toBeNull();

    const menu = await openReviewMenu(user);
    const items = within(menu).getAllByRole("menuitem");
    // 비활성 항목은 「이름 + 사유」 두 줄이다 — 이름 줄만 비교한다
    const labelOf = (i: HTMLElement) => i.querySelector(".flex-col > span")?.textContent ?? i.textContent;
    expect(items.map(labelOf)).toEqual(["내용 보기", "승인", "거절"]);
    const approve = within(menu).getByRole("menuitem", { name: /^승인/ });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveTextContent("내용을 확인한 뒤 승인할 수 있습니다");
    await user.click(approve);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("< 768: 메뉴 「내용 보기」로 같은 검토 상세를 열고 필수 파일을 모두 연 뒤에만 메뉴 「승인」이 켜져 승인한다", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    renderPage();
    const queue = screen.getByRole("heading", { level: 2, name: "검토 요청" }).closest("section")!;
    await within(queue).findByText("양식 채우기");
    await waitFor(() => expect(within(queue).queryByRole("button", { name: "내용 보기" })).toBeNull());

    let menu = await openReviewMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "내용 보기" }));
    expect(await screen.findByTestId("review-version-panel")).toBeInTheDocument();
    await screen.findByText(/name: form-filler/);
    // SKILL.md만으로는 부족하다(스크립트 허가 요청) — 메뉴 승인은 아직 잠김
    menu = await openReviewMenu(user);
    expect(within(menu).getByRole("menuitem", { name: /^승인/ })).toHaveAttribute("aria-disabled", "true");
    expect(within(menu).getByRole("menuitem", { name: "내용 닫기" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: /scripts\/fill\.py/ }));
    await screen.findByText("import os");
    menu = await openReviewMenu(user);
    const approve = within(menu).getByRole("menuitem", { name: "승인" });
    await waitFor(() => expect(approve).not.toHaveAttribute("aria-disabled", "true"));
    expect(approve).not.toHaveTextContent("내용을 확인한 뒤");
    await user.click(approve);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/skills/review-requests/r1/approve"),
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("검토 요청을 승인했습니다", expect.anything()));
  });

  it("< 768: 메뉴 「거절」은 같은 사유 입력 대화를 열고(1클릭 거절 없음) note와 함께 보내며, 취소하면 0회·⋯로 포커스 복귀", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("form-filler");
    await waitFor(() => expect(screen.queryByRole("button", { name: "거절" })).toBeNull());

    let menu = await openReviewMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "거절" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "검토 요청을 거절할까요?" })).toBeInTheDocument();
    await act(() => new Promise((r) => setTimeout(r, 50)));
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.post).not.toHaveBeenCalled();
    await waitFor(() => expect(reviewMore()).toHaveFocus());

    menu = await openReviewMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "거절" }));
    dialog = await screen.findByRole("dialog");
    expect(api.post).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText("거절 사유(선택)"), { target: { value: "  외부 전송 코드  " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "거절" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/skills/review-requests/r1/reject", {
        note: "외부 전송 코드",
      }),
    );
  });
});
