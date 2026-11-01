/**
 * 스킬 목록 페이지 — 기준선 추적표 #49~#52(계획 AC-21 (a)) + ui-ux-redesign P4(AC-4.1·4.2·4.4, plan §6.6.2 스킬 목록).
 * 경계(`@/lib/api-client`·라우터·sonner 토스트)만 대체하고 페이지·카드·다이얼로그·훅·더보기 메뉴는 실제로 렌더한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillsPage from "@/app/(app)/skills/page";
import { skillCardStatus } from "@/components/skills/skill-card";
import { SKILL_VISIBILITY_LABEL, skillVisibilityLabel } from "@/lib/labels";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), post: vi.fn(), postForm: vi.fn() },
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

function skill(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    name: `스킬 ${id}`,
    description: "",
    visibility: "private",
    can_run: false,
    can_edit: true,
    published_version_id: null,
    forked_from_version_id: null,
    clone_count: 0,
    run_count: 0,
    ...extra,
  };
}

function renderPage(skills: unknown[] | Error, catalog: unknown = { enabled: false, entries: [] }) {
  m.get.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/v1/skills/catalog")) return catalog;
    if (skills instanceof Error) throw skills;
    return skills;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SkillsPage />
    </QueryClientProvider>,
  );
}

function chooseZip(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["zip"], "pack.zip", { type: "application/zip" });
  fireEvent.change(input, { target: { files: [file] } });
  return file;
}

/** 카드(li) 목록 */
async function cards() {
  const grid = await screen.findByTestId("skill-grid");
  return within(grid).getAllByRole("listitem");
}

afterEach(() => {
  m.get.mockReset();
  m.post.mockReset();
  m.postForm.mockReset();
  push.mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("SkillCard (#49, P4 AC-4.4)", () => {
  it("실행 가능·공개 중·카탈로그 상태를 공개 포인터와 카탈로그 항목으로 표시하고, 미실행 스킬은 「초안」이다", async () => {
    renderPage(
      [
        skill("a", { can_run: true, published_version_id: "v1", visibility: "org" }),
        skill("b"),
        skill("c", { can_run: true }),
      ],
      {
        enabled: true,
        entries: [
          { skill_id: "a", shadowed_by: null },
          { skill_id: "c", shadowed_by: "a" },
        ],
      },
    );

    const [a, b, c] = await cards();
    expect(a).toHaveTextContent("실행 가능");
    expect(a).toHaveTextContent("공개 중");
    expect(a).toHaveTextContent("채팅 카탈로그");
    expect(a).toHaveTextContent("조직 공유");
    expect(b).toHaveTextContent("초안");
    expect(b).not.toHaveTextContent("채팅 카탈로그");
    expect(c).toHaveTextContent("가려짐");
    // 카드 제목 링크가 상세로 간다(카드 전체를 덮는 링크)
    expect(within(a).getByRole("link", { name: "스킬 a" })).toHaveAttribute("href", "/skills/a");
    // 상태는 텍스트가 있는 StatusBadge다(DP-4) — 「실행 가능」은 success 톤
    expect(within(a).getByText("실행 가능")).toHaveAttribute("data-tone", "success");
    expect(within(b).getByText("초안")).toHaveAttribute("data-tone", "neutral");
    // 「공개 중」은 관리 스킬 공개 화면과 같은 success 톤이다(P6 — 진단 P-07 ③, SKILL_STATUS_TONE)
    expect(within(a).getByText("공개 중")).toHaveAttribute("data-tone", "success");
  });

  it("원시값 draft·빌트인을 화면에 보이지 않는다 — 기본 제공 스킬은 「기본 제공」 배지(AC-4.4, §6.3.1)", async () => {
    const { container } = renderPage([skill("s", { visibility: "system", can_edit: false }), skill("d")]);
    const [system] = await cards();
    expect(system).toHaveTextContent("기본 제공");
    expect(container.textContent).not.toMatch(/draft/i);
    expect(container.textContent).not.toContain("빌트인");
  });

  it("메타는 「복제 N · 실행 N」 천 단위 쉼표, 복제해 만든 스킬은 「복제본」", async () => {
    renderPage([skill("a", { clone_count: 1234, run_count: 5, forked_from_version_id: "v0" })]);
    const [a] = await cards();
    expect(a).toHaveTextContent("복제 1,234 · 실행 5 · 복제본");
  });

  it("카탈로그가 꺼져 있으면(표준 런타임 off) 카탈로그 배지를 내지 않는다", async () => {
    renderPage([skill("a", { can_run: true })], {
      enabled: false,
      entries: [{ skill_id: "a", shadowed_by: null }],
    });
    const grid = await screen.findByTestId("skill-grid");
    expect(grid).not.toHaveTextContent("채팅 카탈로그");
  });

  it("skillCardStatus·skillVisibilityLabel: 알려진 값은 라벨, 미지 공개 범위는 원시값", () => {
    expect(skillCardStatus({ can_run: true })).toEqual({ label: "실행 가능", tone: "success" });
    expect(skillCardStatus({ can_run: false })).toEqual({ label: "초안", tone: "neutral" });
    expect(SKILL_VISIBILITY_LABEL).toEqual({ private: "비공개", org: "조직 공유", system: "기본 제공" });
    expect(skillVisibilityLabel("system")).toBe("기본 제공");
    expect(skillVisibilityLabel("team")).toBe("team");
  });
});

describe("카드 더보기 — 복제·편집(plan §6.6.2, Tier 1 비인가 관점)", () => {
  it("편집 권한이 있으면 「복제 · 편집」, 없으면 「복제」만 보인다", async () => {
    const user = userEvent.setup();
    renderPage([skill("mine"), skill("theirs", { can_edit: false, visibility: "org" })]);
    await cards();

    await user.click(screen.getByRole("button", { name: "스킬 mine 동작 더보기" }));
    let menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["복제", "편집"]);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "스킬 theirs 동작 더보기" }));
    menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["복제"]);
  });

  it("「편집」은 편집 화면으로, 「복제」는 clone API 1회 뒤 새 스킬 편집 화면으로 간다", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue({ skill_id: "copy-1", status: "draft" });
    renderPage([skill("mine")]);
    await cards();

    await user.click(screen.getByRole("button", { name: "스킬 mine 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "편집" }));
    expect(push).toHaveBeenCalledWith("/skills/mine/edit");

    await user.click(screen.getByRole("button", { name: "스킬 mine 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "복제" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/copy-1/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills/mine/clone");
    expect(toast.success).toHaveBeenCalledWith("스킬을 복제했습니다", expect.anything());
  });

  it("복제가 서버에서 거부되면(403) 실패 토스트를 보이고 이동하지 않는다", async () => {
    const user = userEvent.setup();
    m.post.mockRejectedValue(new ApiError(403, "forbidden"));
    renderPage([skill("theirs", { can_edit: false })]);
    await cards();
    await user.click(screen.getByRole("button", { name: "스킬 theirs 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "복제" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("스킬을 복제하지 못했습니다", expect.anything()),
    );
    expect(push).not.toHaveBeenCalled();
  });
});

describe("헤더 — PageHeader actions, 주 버튼 1개(AC-4.1, DP-2)", () => {
  it("「ZIP 가져오기」(outline) → 「새 스킬」(주 버튼) 순서이고 채움(primary) 버튼은 하나다", async () => {
    const { container } = renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");
    expect(screen.getByRole("heading", { level: 1, name: "스킬" })).toBeInTheDocument();
    const actions = container.querySelector<HTMLElement>("[data-slot=page-header-actions]")!;
    const buttons = within(actions).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["ZIP 가져오기", "새 스킬"]);
    expect(buttons.filter((b) => b.classList.contains("bg-primary"))).toEqual([buttons[1]]);
  });
});

describe("새 스킬 — 이름 입력 다이얼로그(AC-4.2, HCI-3 ②)", () => {
  it("다이얼로그를 여는 것만으로는 만들지 않고, 이름이 비면(공백만 포함) 「만들기」가 비활성이다", async () => {
    const user = userEvent.setup();
    renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");

    await user.click(screen.getByRole("button", { name: "새 스킬" }));
    const dialog = await screen.findByRole("dialog", { name: "새 스킬" });
    const submit = within(dialog).getByRole("button", { name: "만들기" });
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByLabelText("이름"), "   ");
    expect(submit).toBeDisabled();
    expect(m.post).not.toHaveBeenCalled();
  });

  it("이름·설명을 넣고 「만들기」를 누르면 POST 1회(앞뒤 공백 제거) 뒤 편집 화면으로 간다", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue({ id: "new-skill", name: "회의록 요약" });
    renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");

    await user.click(screen.getByRole("button", { name: "새 스킬" }));
    const dialog = await screen.findByRole("dialog", { name: "새 스킬" });
    await user.type(within(dialog).getByLabelText("이름"), "  회의록 요약 ");
    await user.type(within(dialog).getByLabelText("설명 (선택)"), "회의록을 요약한다");
    await user.click(within(dialog).getByRole("button", { name: "만들기" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/new-skill/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills", {
      name: "회의록 요약",
      description: "회의록을 요약한다",
    });
    expect(toast.success).toHaveBeenCalledWith("스킬을 만들었습니다", expect.anything());
  });

  it("생성이 실패하면 실패 토스트를 보이고 다이얼로그를 유지한다", async () => {
    const user = userEvent.setup();
    m.post.mockRejectedValue(new ApiError(500, "boom"));
    renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");
    await user.click(screen.getByRole("button", { name: "새 스킬" }));
    const dialog = await screen.findByRole("dialog", { name: "새 스킬" });
    await user.type(within(dialog).getByLabelText("이름"), "x");
    await user.click(within(dialog).getByRole("button", { name: "만들기" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("스킬을 생성하지 못했습니다", expect.anything()),
    );
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "새 스킬" })).toBeInTheDocument();
  });
});

describe("빈·검색 결과 없음·오류 상태(§6.5)", () => {
  it("스킬이 없으면 빈 상태 — 생성 진입점은 헤더 하나뿐이다(DP-2)", async () => {
    renderPage([]);
    expect(await screen.findByText("아직 스킬이 없습니다")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "새 스킬" })).toHaveLength(1);
  });

  it("검색 결과가 없으면 「조건에 맞는 항목이 없습니다」 + 「필터 지우기」로 검색어를 지운다(서버 검색 q)", async () => {
    const user = userEvent.setup();
    renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");
    await user.type(screen.getByRole("searchbox", { name: "스킬 검색" }), "없음");
    expect(await screen.findByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    expect(m.get).toHaveBeenCalledWith(expect.stringContaining("/api/v1/skills?q="));
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(screen.getByRole("searchbox", { name: "스킬 검색" })).toHaveValue("");
    expect(await screen.findByText("아직 스킬이 없습니다")).toBeInTheDocument();
  });

  it("목록을 못 불러오면 ErrorState와 「다시 시도」(재요청)", async () => {
    const user = userEvent.setup();
    renderPage(new Error("down"));
    expect(await screen.findByText("스킬 목록을 불러오지 못했습니다")).toBeInTheDocument();
    const before = m.get.mock.calls.filter(([p]) => p === "/api/v1/skills").length;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() =>
      expect(m.get.mock.calls.filter(([p]) => p === "/api/v1/skills").length).toBe(before + 1),
    );
  });

  it("「다시 시도」 뒤 포커스는 페이지 래퍼 안 첫 버튼 「ZIP 가져오기」에 놓인다 — 숨은 파일 입력이 아니다(HCI-P2-2)", async () => {
    const user = userEvent.setup();
    let listReply: () => Promise<unknown> = () => Promise.reject(new Error("down"));
    m.get.mockImplementation((path: string) =>
      path.startsWith("/api/v1/skills/catalog")
        ? Promise.resolve({ enabled: false, entries: [] })
        : listReply(),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={client}>
        <SkillsPage />
      </QueryClientProvider>,
    );
    await screen.findByText("스킬 목록을 불러오지 못했습니다");
    let release: (value: unknown) => void = () => {};
    listReply = () => new Promise((resolve) => (release = resolve));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    // 오류 → 스켈레톤: 포커스는 사라지지 않고 래퍼 안 첫 버튼으로 간다(ErrorState 기본 착지)
    expect(await screen.findByText("스킬 목록을 불러오는 중")).toBeInTheDocument();
    const zip = screen.getByRole("button", { name: "ZIP 가져오기" });
    await waitFor(() => expect(document.activeElement).toBe(zip));
    release([skill("a")]);
    expect(await screen.findByText("스킬 a")).toBeInTheDocument();
    expect(document.activeElement).toBe(zip);
    // 숨은 파일 입력은 페이지 래퍼(PageContainer) 밖에 있다
    const wrapper = container.querySelector('[data-slot="page-container"]') as HTMLElement;
    expect(wrapper.querySelector('input[type="file"]')).toBeNull();
    expect(container.querySelector('input[type="file"][accept=".zip"]')).not.toBeNull();
  });
});

describe("ZIP 가져오기 (#50~#52)", () => {
  it("SKILL.md가 하나면 후보 확인 뒤 바로 가져오고 보고서 요약을 토스트로 보인다", async () => {
    const { container } = renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");
    m.postForm.mockImplementation(async (path: string) =>
      path.endsWith("/candidates")
        ? { candidates: [{ path: "pack/SKILL.md" }] }
        : {
            id: "new",
            name: "가져온 스킬",
            import_report: {
              standard: { errors: ["a", "b"] },
              root_mismatch: { code: "root", message: "m" },
              policy: { warnings: [{ code: "license_restricts_redistribution", message: "x" }] },
            },
          },
    );

    chooseZip(container);

    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/new/edit"));
    const paths = m.postForm.mock.calls.map(([p]) => p);
    expect(paths).toEqual(["/api/v1/skills/import-zip/candidates", "/api/v1/skills/import-zip"]);
    const fd = m.postForm.mock.calls[1][1] as FormData;
    expect(fd.has("selected_skill_path")).toBe(false);
    expect(toast.success).toHaveBeenCalledWith(
      "‘가져온 스킬’ 스킬을 가져왔습니다",
      expect.objectContaining({
        description:
          "표준 위반 2건(보고만) · 루트 폴더 이름이 스킬 이름과 다릅니다 · 라이선스에 재배포 제한 표현이 있습니다",
      }),
    );
  });

  it("SKILL.md가 여럿이면 하나를 고르게 하고 고른 경로만 가져온다", async () => {
    const { container } = renderPage([]);
    await screen.findByText("아직 스킬이 없습니다");
    m.postForm.mockImplementation(async (path: string) =>
      path.endsWith("/candidates")
        ? { candidates: [{ path: "pack/a/SKILL.md" }, { path: "pack/b/SKILL.md" }] }
        : { id: "b", name: "B", import_report: {} },
    );

    chooseZip(container);

    fireEvent.click(await screen.findByRole("button", { name: "pack/b/SKILL.md" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/skills/b/edit"));
    const fd = m.postForm.mock.calls[1][1] as FormData;
    expect(fd.get("selected_skill_path")).toBe("pack/b/SKILL.md");
    expect(toast.success).toHaveBeenCalledWith(
      "‘B’ 스킬을 가져왔습니다",
      expect.objectContaining({ description: undefined }),
    );
  });
});
