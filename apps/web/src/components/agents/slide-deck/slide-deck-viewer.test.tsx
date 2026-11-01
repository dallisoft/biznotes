/**
 * 슬라이드 자료 뷰어(docs/plans/slide-deck-studio.md AC-6~11) — 인라인 목록·대체 표시·확대 보기·발표 모드·PDF 인쇄·PPTX.
 * 삽화(core-v0.23.0-upgrade AC-10)는 산출물 내려받기 경계(`fetchArtifactObjectUrl`)만 대역한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import JSZip from "jszip";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { SlideDeckViewer } from "@/components/agents/slide-deck/slide-deck-viewer";
import { slideKeyTarget } from "@/components/agents/slide-deck/slide-presenter";
import { SLIDE_PRINTING_CLASS } from "@/components/agents/slide-deck/slide-print";
import type { AgentRun } from "@/lib/agent-types";

const DECK = {
  title: "LLM 위키",
  format: "presenter",
  slides: [
    { layout: "title", heading: "안드레이 카파시의 LLM 위키", subheading: "설계와 구현", notes: "인사와 소개" },
    { layout: "section", heading: "1부: 문제", subheading: "왜 무너지는가" },
    { layout: "bullets", heading: "붕괴의 법칙", body: "관리 비용이 커진다.", bullets: ["수집", "구조화"] },
    {
      layout: "two_column",
      heading: "RAG vs LLM 위키",
      columns: [
        { heading: "RAG", bullets: ["매번 조립"] },
        { heading: "LLM 위키", bullets: ["복리 축적"] },
      ],
    },
    { layout: "steps", heading: "수집 단계", steps: [{ label: "분석", detail: "문서를 읽는다" }, { label: "갱신" }] },
    { layout: "stats", heading: "사례", stats: [{ value: "2,188", label: "문서" }, { value: "$50", label: "비용" }] },
    { layout: "quote", heading: "핵심", quote: { text: "지식은 복리로 쌓인다", attribution: "카파시" } },
    { layout: "closing", heading: "정리", bullets: ["오늘 시작하기"], notes: "질문을 받는다" },
  ],
};
const BODY = JSON.stringify(DECK);

const { toastError, fetchImage } = vi.hoisted(() => ({ toastError: vi.fn(), fetchImage: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: toastError, success: vi.fn() } }));
vi.mock("@/hooks/use-agents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/use-agents")>()),
  fetchArtifactObjectUrl: fetchImage,
}));

function renderViewer(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
  toastError.mockReset();
  fetchImage.mockReset();
  document.documentElement.classList.remove(SLIDE_PRINTING_CLASS);
});

describe("인라인 목록(AC-7)", () => {
  it("모든 슬라이드를 순서대로 layout별로 그리고, 발표 노트는 접어 둔다", () => {
    renderViewer(<SlideDeckViewer content={BODY} />);
    const viewer = screen.getByTestId("slide-deck-viewer");
    expect(within(viewer).getByText("발표자 슬라이드 · 슬라이드 8장")).toBeInTheDocument();
    const slides = within(viewer).getAllByRole("group", { name: /^슬라이드 \d+ \/ 8/ });
    expect(slides.map((s) => s.getAttribute("data-layout"))).toEqual(DECK.slides.map((s) => s.layout));
    expect(slides[0]).toHaveAccessibleName("슬라이드 1 / 8: 안드레이 카파시의 LLM 위키");
    // 목록·썸네일 버튼 안에서도 가운데 정렬을 물려받지 않는다(라이브 확인에서 찾은 회귀)
    expect(slides[0].closest("button")).not.toBeNull();
    expect(slides[0]).toHaveClass("text-left");
    expect(within(slides[3]).getByText("LLM 위키")).toBeInTheDocument();
    expect(within(slides[4]).getByText("문서를 읽는다")).toBeInTheDocument();
    expect(within(slides[5]).getByText("$50")).toBeInTheDocument();
    expect(within(slides[6]).getByText("— 카파시")).toBeInTheDocument();
    expect(within(viewer).getAllByText("발표 노트")).toHaveLength(2);
  });

  it("형식이 아니면 사유와 원문을 보인다", () => {
    renderViewer(<SlideDeckViewer content='{"title": "t", "slides": [{"layout": "title"}]}' />);
    const fallback = screen.getByTestId("slide-deck-fallback");
    expect(fallback).toHaveTextContent("slides[0].heading이 비어있습니다");
    expect(fallback).toHaveTextContent('"layout": "title"');
  });

  it("본문의 마크업은 텍스트로만 그린다", () => {
    const hostile = { title: "t", slides: [{ heading: "<img src=x onerror=alert(1)>", bullets: ["<b>굵게</b>"] }] };
    const { container } = renderViewer(<SlideDeckViewer content={JSON.stringify(hostile)} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<b>굵게</b>")).toBeInTheDocument();
  });
});

describe("소스 반영 표시(#218 — 대체 본문·미반영 소스·만들지 못한 장, 장마다 근거)", () => {
  const COVERED = {
    ...DECK,
    slides: DECK.slides.slice(0, 3),
    coverage: {
      sources: [
        { id: "a", title: "카파시 LLM 위키", cached: true },
        { id: "b", title: "OKF 표준", cached: true },
        { id: "c", title: "회의 메모", cached: false },
        { id: "d", title: "<b>빈 파일</b>", cached: false },
      ],
      degraded: [{ id: "d", title: "<b>빈 파일</b>", reason: "no_content", fallback: "none" }],
      assigned: [["a"], ["a", "b"], ["b"]],
      cited: [["a"], ["a", "b"], []],
      outline: ["표지", "1부", "붕괴의 법칙", "정리"],
      failed: [{ index: 3, error_code: "agent_runtime_error", error: "x" }],
    },
  };

  it("덱 위에 반영 요약을 두고 미반영 소스·대체 본문·만들지 못한 장을 나열한다", () => {
    const { container } = renderViewer(<SlideDeckViewer content={JSON.stringify(COVERED)} />);
    const panel = screen.getByTestId("slide-deck-coverage");
    expect(within(panel).getByTestId("slide-coverage-summary")).toHaveTextContent(
      "고른 소스 4개 중 2개를 장에 반영했습니다.",
    );
    const uncovered = within(panel).getByTestId("slide-coverage-uncovered");
    expect(uncovered).toHaveTextContent("어느 장에도 쓰이지 않은 소스 2개");
    expect(within(uncovered).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "회의 메모",
      "<b>빈 파일</b>",
    ]);
    expect(within(panel).getByTestId("slide-coverage-degraded")).toHaveTextContent(
      "요약 대신 원문 일부로 읽은 소스 1개<b>빈 파일</b> (본문 없음)",
    );
    expect(within(panel).getByTestId("slide-coverage-failed")).toHaveTextContent("만들지 못한 장 1개4. 정리");
    expect(container.querySelector("b")).toBeNull(); // 소스 제목은 텍스트로만 그린다
  });

  it("장마다 근거 소스를 슬라이드 아래에 보이고, 근거가 없는 장은 비운다 — 확대 보기도 같다", async () => {
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={JSON.stringify(COVERED)} />);
    const viewer = screen.getByTestId("slide-deck-viewer");
    expect(within(viewer).getAllByTestId("slide-sources").map((n) => n.textContent)).toEqual([
      "근거: 카파시 LLM 위키",
      "근거: 카파시 LLM 위키 · OKF 표준",
    ]);
    await user.click(screen.getByRole("button", { name: "슬라이드 2 크게 보기" }));
    const dialog = await screen.findByTestId("slide-deck-expanded");
    expect(within(dialog).getByTestId("slide-sources")).toHaveTextContent("근거: 카파시 LLM 위키 · OKF 표준");
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(within(dialog).queryByTestId("slide-sources")).toBeNull();
  });

  it("모두 반영했으면 경고 없이 한 줄 — 옛 덱(coverage 없음)은 표시가 없다", () => {
    const all = {
      ...COVERED,
      coverage: { ...COVERED.coverage, sources: COVERED.coverage.sources.slice(0, 2), degraded: [], failed: [] },
    };
    const { unmount } = renderViewer(<SlideDeckViewer content={JSON.stringify(all)} />);
    const panel = screen.getByTestId("slide-deck-coverage");
    expect(panel).toHaveTextContent("고른 소스 2개를 모두 장에 반영했습니다.");
    expect(within(panel).queryByTestId("slide-coverage-uncovered")).toBeNull();
    expect(panel).not.toHaveClass("bg-warning/10");
    unmount();

    renderViewer(<SlideDeckViewer content={BODY} />);
    expect(screen.queryByTestId("slide-deck-coverage")).toBeNull();
    expect(screen.queryByTestId("slide-sources")).toBeNull();
  });

  it("소스 요약 모델(#239) — 모델별 개수·캐시 여부 한 줄과 소스별 목록. 모델 키가 없는 옛 덱은 줄이 없다", () => {
    const { unmount } = renderViewer(<SlideDeckViewer content={JSON.stringify(COVERED)} />);
    expect(screen.queryByTestId("slide-coverage-digest")).toBeNull(); // COVERED는 v0.27.0 전 덱
    unmount();

    const withModels = {
      ...COVERED,
      coverage: {
        ...COVERED.coverage,
        sources: [
          { id: "a", title: "카파시 LLM 위키", cached: true, model_id: "m1", model_name: "gemini-3.8-flash" },
          { id: "b", title: "OKF 표준", cached: false, model_id: "m2", model_name: "gpt-oss-120b" },
          { id: "c", title: "회의 메모", cached: true, model_id: "m2", model_name: "gpt-oss-120b" },
          { id: "d", title: "<b>빈 파일</b>", cached: false, model_id: null, model_name: null },
        ],
      },
    };
    const { container } = renderViewer(<SlideDeckViewer content={JSON.stringify(withModels)} />);
    const digest = screen.getByTestId("slide-coverage-digest");
    // 많은 순 — 일부만 캐시면 캐시 수, 모두 캐시면 「(캐시)」. 대체 본문(모델 없음)은 세지 않는다
    expect(digest.querySelector("summary")).toHaveTextContent(
      "요약: gpt-oss-120b 2개(캐시 1) · gemini-3.8-flash 1개(캐시)",
    );
    expect(within(digest).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "카파시 LLM 위키 — gemini-3.8-flash(캐시)",
      "OKF 표준 — gpt-oss-120b",
      "회의 메모 — gpt-oss-120b(캐시)",
      "<b>빈 파일</b> — 요약 없음",
    ]);
    expect(container.querySelector("b")).toBeNull();
  });
});

describe("확대 보기(AC-8)", () => {
  it("슬라이드를 누르면 그 장부터 크게 보이고, 썸네일·화살표 키로 넘긴다", async () => {
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "슬라이드 2 크게 보기" }));
    const dialog = await screen.findByTestId("slide-deck-expanded");
    expect(within(dialog).getByText("슬라이드 2 / 8")).toBeInTheDocument();
    const main = within(dialog).getByTestId("slide-deck-expanded-main");
    expect(within(main).getByRole("group")).toHaveAttribute("data-layout", "section");

    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(within(dialog).getByText("슬라이드 3 / 8")).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: "End" });
    expect(within(dialog).getByText("슬라이드 8 / 8")).toBeInTheDocument();
    expect(within(dialog).getByText("질문을 받는다")).toBeInTheDocument();

    const thumbs = within(dialog).getByRole("list", { name: "슬라이드 썸네일" });
    await user.click(within(thumbs).getByRole("button", { name: "슬라이드 1로 이동" }));
    expect(within(dialog).getByText("슬라이드 1 / 8")).toBeInTheDocument();
    expect(within(thumbs).getByRole("button", { name: "슬라이드 1로 이동" })).toHaveAttribute("aria-current", "true");
  });
});

describe("발표 모드(AC-9)", () => {
  it("전체 화면을 요청하고 키보드로 넘기며 N은 노트, Esc는 끝낸다", async () => {
    const request = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { configurable: true, value: request });
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "발표 시작" }));
    const stage = screen.getByTestId("slide-presenter");
    expect(request).toHaveBeenCalledTimes(1);
    expect(stage).toHaveFocus();
    expect(within(stage).getByRole("group")).toHaveAccessibleName(/^슬라이드 1 \/ 8/);

    fireEvent.keyDown(stage, { key: "n" });
    expect(within(stage).getByTestId("slide-presenter-notes")).toHaveTextContent("인사와 소개");
    fireEvent.keyDown(stage, { key: " " });
    expect(within(stage).getByRole("group")).toHaveAccessibleName(/^슬라이드 2 \/ 8/);
    expect(within(stage).getByTestId("slide-presenter-notes")).toHaveTextContent("발표 노트가 없습니다");
    fireEvent.keyDown(stage, { key: "ArrowLeft" });
    fireEvent.keyDown(stage, { key: "ArrowLeft" });
    expect(within(stage).getByRole("group")).toHaveAccessibleName(/^슬라이드 1 \/ 8/);

    fireEvent.keyDown(stage, { key: "Escape" });
    expect(screen.queryByTestId("slide-presenter")).toBeNull();
    delete (HTMLElement.prototype as { requestFullscreen?: unknown }).requestFullscreen;
  });

  it("오버레이에서 Tab은 무대 안에서 돌고, 포커스가 밖에 있어도 Esc로 끝나며, 연 버튼으로 포커스가 돌아온다", async () => {
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    const opener = screen.getByRole("button", { name: "발표 시작" });
    await user.click(opener);
    const stage = screen.getByTestId("slide-presenter");
    const buttons = within(stage).getAllByRole("button").filter((b) => !(b as HTMLButtonElement).disabled);
    // 첫 장이라 「이전」은 꺼져 있다 — 노트·다음·끝내기 사이에서 돈다
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual([
      "발표 노트 보기 (N)",
      "다음 슬라이드",
      "발표 끝내기 (Esc)",
    ]);
    fireEvent.keyDown(stage, { key: "Tab" });
    expect(buttons[0]).toHaveFocus();
    fireEvent.keyDown(buttons[0], { key: "Tab", shiftKey: true });
    expect(buttons[2]).toHaveFocus();
    fireEvent.keyDown(buttons[2], { key: "Tab" });
    expect(buttons[0]).toHaveFocus();

    act(() => document.body.focus());
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByTestId("slide-presenter")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("조작 버튼 위의 Space는 그 버튼의 동작이다 — 슬라이드를 넘기지 않는다", async () => {
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "발표 시작" }));
    const stage = screen.getByTestId("slide-presenter");
    const notesButton = within(stage).getByRole("button", { name: "발표 노트 보기 (N)" });
    act(() => notesButton.focus());
    await user.keyboard(" ");
    expect(within(stage).getByRole("group")).toHaveAccessibleName(/^슬라이드 1 \/ 8/);
    expect(within(stage).getByTestId("slide-presenter-notes")).toHaveTextContent("인사와 소개");
  });

  it("Fullscreen API가 없으면 오버레이로 발표한다", async () => {
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "발표 시작" }));
    expect(screen.getByTestId("slide-presenter")).toBeInTheDocument();
  });

  it("넘김 키 — 끝에서 멈추고, 모르는 키는 null", () => {
    expect(slideKeyTarget("ArrowRight", 7, 8)).toBe(7);
    expect(slideKeyTarget("PageUp", 0, 8)).toBe(0);
    expect(slideKeyTarget("Home", 5, 8)).toBe(0);
    expect(slideKeyTarget("PageDown", 2, 8)).toBe(3);
    expect(slideKeyTarget("a", 2, 8)).toBeNull();
  });
});

describe("PDF 인쇄(AC-10)", () => {
  it("인쇄용 덱(한 장에 한 슬라이드)을 붙이고 인쇄한 뒤 afterprint에 걷는다", async () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "PDF로 인쇄" }));
    const root = document.querySelector("[data-slide-print-root]");
    expect(root?.parentElement).toBe(document.body);
    expect(root?.querySelectorAll(".slide-print-page")).toHaveLength(8);
    expect(document.documentElement).toHaveClass(SLIDE_PRINTING_CLASS);
    await vi.waitFor(() => expect(print).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event("afterprint"));
    });
    expect(document.querySelector("[data-slide-print-root]")).toBeNull();
    expect(document.documentElement).not.toHaveClass(SLIDE_PRINTING_CLASS);
  });
});

describe("PPTX 다운로드(AC-11)", () => {
  function stubDownload(createObjectURL: (b: Blob) => string) {
    const saved: { blob: Blob; download: string }[] = [];
    const blobs = new Map<string, Blob>();
    // URL 전체를 바꾸면 Vitest의 동적 import(pptxgenjs)가 깨진다 — jsdom에 없는 두 정적 메서드만 단다
    vi.spyOn(URL, "createObjectURL").mockImplementation((b) => {
      const u = createObjectURL(b as Blob);
      blobs.set(u, b as Blob);
      return u;
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ blob: blobs.get(this.href) as Blob, download: this.download });
    });
    return saved;
  }

  it("도구 막대와 확대 보기에서 덱 제목의 .pptx를 내려받는다", async () => {
    const saved = stubDownload(() => "blob:deck");
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "PPTX 다운로드" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].download).toBe("LLM 위키.pptx");
    const zip = await JSZip.loadAsync(await saved[0].blob.arrayBuffer());
    expect(Object.keys(zip.files).filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))).toHaveLength(8);
    await waitFor(() => expect(screen.getByRole("button", { name: "PPTX 다운로드" })).toBeEnabled());

    await user.click(screen.getByRole("button", { name: "크게 보기" }));
    const dialog = await screen.findByTestId("slide-deck-expanded");
    await user.click(within(dialog).getByRole("button", { name: "PPTX 다운로드" }));
    await waitFor(() => expect(saved).toHaveLength(2));
    expect(toastError).not.toHaveBeenCalled();
  });

  it("도구 막대 버튼은 터치 기기에서 44px 타깃이다(리뷰 L1)", () => {
    renderViewer(<SlideDeckViewer content={BODY} />);
    for (const name of ["발표 시작", "크게 보기", "PDF로 인쇄", "PPTX 다운로드"]) {
      expect(screen.getByRole("button", { name })).toHaveClass("coarse:size-11");
    }
  });

  it("만들다 실패하면 오류 토스트를 띄우고 버튼을 다시 켠다", async () => {
    const saved = stubDownload(() => {
      throw new Error("no blob url");
    });
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} />);
    await user.click(screen.getByRole("button", { name: "PPTX 다운로드" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("PPTX 파일을 만들지 못했습니다"));
    expect(saved).toHaveLength(0);
    expect(screen.getByRole("button", { name: "PPTX 다운로드" })).toBeEnabled();
  });
});

describe("삽화(core-v0.23.0-upgrade AC-10)", () => {
  const ref = (name: string, content_type = "image/png") => ({ key: `run-deck/${name}`, name, content_type });
  const RUN = {
    id: "run-deck",
    status: "succeeded",
    artifact_keys: [
      ref("slide-deck.json", "application/json"),
      ref("slide-image-1.png"),
      ref("slide-image-3.png"), // 내려받기 실패 — 이미지 없이 그린다
      ref("slide-image-8.jpg", "image/jpeg"),
      ref("slide-image-9.png"), // 덱 밖 — 요청하지 않는다
    ],
  } as unknown as AgentRun;

  function images() {
    fetchImage.mockImplementation(async (_run: string, name: string) => {
      if (name === "slide-image-3.png") throw new Error("404");
      return `blob:${name}`;
    });
  }

  function slideImage(slide: HTMLElement): string | null {
    return within(slide).queryByTestId("slide-image")?.getAttribute("src") ?? null;
  }

  it("목록에서 산출물 번호의 장에만 오른쪽 40% 칸으로 붙고, 받지 못한 장은 이미지 없이 그린다", async () => {
    images();
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const viewer = screen.getByTestId("slide-deck-viewer");
    const slides = within(viewer).getAllByRole("group", { name: /^슬라이드 \d+ \/ 8/ });
    await waitFor(() => expect(slideImage(slides[0])).toBe("blob:slide-image-1.png"));
    await waitFor(() => expect(slideImage(slides[7])).toBe("blob:slide-image-8.jpg"));
    expect(slides[0]).toHaveAttribute("data-image", "true");
    const img = within(slides[0]).getByTestId("slide-image");
    expect(img).toHaveClass("left-[60cqw]", "w-[37.8cqw]", "object-cover");
    expect(img).toHaveAttribute("alt", "");
    // 본문은 왼쪽 60%(오른쪽 43cqw 비움), 그림이 없는 장은 지금과 같다
    expect(within(slides[0]).getByRole("heading").closest(".pl-\\[6cqw\\]")).toHaveClass("pr-[43cqw]");
    for (const i of [1, 2, 3, 4, 5, 6]) {
      expect(slideImage(slides[i])).toBeNull();
      expect(slides[i]).not.toHaveAttribute("data-image");
    }
    expect(fetchImage.mock.calls.map(([, name]) => name).sort()).toEqual([
      "slide-image-1.png",
      "slide-image-3.png",
      "slide-image-8.jpg",
    ]);
  });

  it("그림이 깨지면(디코드 실패) 그 장은 이미지 없이 다시 그린다", async () => {
    images();
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const [first] = screen.getAllByRole("group", { name: /^슬라이드 1 \/ 8/ });
    const img = await within(first).findByTestId("slide-image");
    fireEvent.error(img);
    expect(within(first).queryByTestId("slide-image")).toBeNull();
    expect(first).not.toHaveAttribute("data-image");
  });

  it("확대 보기·썸네일·발표·인쇄에도 같은 장에 붙는다", async () => {
    images();
    vi.spyOn(window, "print").mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const [first] = screen.getAllByRole("group", { name: /^슬라이드 1 \/ 8/ });
    await within(first).findByTestId("slide-image");

    await user.click(screen.getByRole("button", { name: "슬라이드 1 크게 보기" }));
    const dialog = await screen.findByTestId("slide-deck-expanded");
    expect(slideImage(within(dialog).getByTestId("slide-deck-expanded-main"))).toBe("blob:slide-image-1.png");
    const thumbs = within(within(dialog).getByRole("list", { name: "슬라이드 썸네일" })).getAllByRole("group");
    expect(thumbs.map(slideImage)).toEqual([
      "blob:slide-image-1.png",
      null,
      null,
      null,
      null,
      null,
      null,
      "blob:slide-image-8.jpg",
    ]);

    await user.click(within(dialog).getByRole("button", { name: "발표 시작" }));
    const presenter = await screen.findByTestId("slide-presenter");
    expect(slideImage(presenter)).toBe("blob:slide-image-1.png");
    await user.keyboard("{End}");
    expect(slideImage(presenter)).toBe("blob:slide-image-8.jpg");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByTestId("slide-presenter")).toBeNull());
    await user.click(screen.getAllByRole("button", { name: "PDF로 인쇄" })[0]);
    const root = document.querySelector<HTMLElement>("[data-slide-print-root]");
    const pages = Array.from(root?.querySelectorAll<HTMLElement>(".slide-print-page") ?? []);
    expect(pages.map((p) => p.querySelector("img")?.getAttribute("src") ?? null)).toEqual([
      "blob:slide-image-1.png",
      null,
      null,
      null,
      null,
      null,
      null,
      "blob:slide-image-8.jpg",
    ]);
    await vi.waitFor(() => expect(window.print).toHaveBeenCalledTimes(1));
  });

  it("인쇄는 그림을 디코드한 다음 프레임에 한다 — 디코드 실패는 기다리지 않는다", async () => {
    images();
    let finish: () => void = () => undefined;
    // jsdom에는 decode가 없다 — 첫 그림은 늦게 끝나고 둘째는 실패한다
    const decode = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)))
      .mockImplementation(() => Promise.reject(new Error("broken")));
    Object.defineProperty(HTMLImageElement.prototype, "decode", { configurable: true, value: decode });
    onTestFinished(() => {
      delete (HTMLImageElement.prototype as { decode?: unknown }).decode;
    });
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const [first] = screen.getAllByRole("group", { name: /^슬라이드 1 \/ 8/ });
    await within(first).findByTestId("slide-image");
    await waitFor(() =>
      expect(screen.getAllByRole("group", { name: /^슬라이드 8 \/ 8/ }).some((s) => slideImage(s))).toBe(true),
    );
    await user.click(screen.getByRole("button", { name: "PDF로 인쇄" }));
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 50));
    expect(print).not.toHaveBeenCalled();
    act(() => finish());
    await vi.waitFor(() => expect(print).toHaveBeenCalledTimes(1));
  });

  it("삽화를 받는 동안 인쇄·PPTX를 끄고, 다 받으면(실패 포함) 켠다 — 그림이 빠진 파일을 만들지 않는다(리뷰 M2)", async () => {
    const pending = new Map<string, (url: string) => void>();
    fetchImage.mockImplementation(
      (_run: string, name: string) =>
        new Promise<string>((resolve, reject) => {
          if (name === "slide-image-3.png") return reject(new Error("404"));
          pending.set(name, resolve);
        }),
    );
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const print = screen.getByRole("button", { name: "PDF로 인쇄" });
    const pptx = screen.getByRole("button", { name: "PPTX 다운로드" });
    await waitFor(() => expect(pending.size).toBe(2));
    expect(print).toBeDisabled();
    expect(pptx).toBeDisabled();
    expect(print).toHaveAttribute("title", "PDF로 인쇄 — 삽화를 불러오는 중입니다");
    // 발표·확대는 기다리지 않는다
    expect(screen.getByRole("button", { name: "발표 시작" })).toBeEnabled();
    act(() => pending.get("slide-image-1.png")?.("blob:1"));
    await waitFor(() => expect(screen.getAllByTestId("slide-image")).toHaveLength(1));
    expect(print).toBeDisabled();
    act(() => pending.get("slide-image-8.jpg")?.("blob:8"));
    await waitFor(() => expect(print).toBeEnabled());
    expect(pptx).toBeEnabled();
    expect(print).toHaveAttribute("title", "PDF로 인쇄");
  });

  it("삽화가 없는 덱은 처음부터 내보낼 수 있다", () => {
    renderViewer(<SlideDeckViewer content={BODY} run={{ ...RUN, artifact_keys: [RUN.artifact_keys[0]] }} />);
    expect(screen.getByRole("button", { name: "PDF로 인쇄" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "PPTX 다운로드" })).toBeEnabled();
    expect(fetchImage).not.toHaveBeenCalled();
  });

  it("PPTX에는 지금 보이는 삽화를 넣는다(읽지 못한 그림은 빼고 파일을 만든다)", async () => {
    images();
    const saved: Blob[] = [];
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const blobSpy = vi.spyOn(URL, "createObjectURL").mockImplementation((b) => {
      saved.push(b as Blob);
      return "blob:deck";
    });
    // jsdom에는 그림 디코드(HTMLImageElement.decode)가 없다 — 그림은 모두 빠지고 파일은 만들어진다
    const user = userEvent.setup();
    renderViewer(<SlideDeckViewer content={BODY} run={RUN} />);
    const [first] = screen.getAllByRole("group", { name: /^슬라이드 1 \/ 8/ });
    await within(first).findByTestId("slide-image");
    await user.click(screen.getByRole("button", { name: "PPTX 다운로드" }));
    await waitFor(() => expect(blobSpy).toHaveBeenCalled());
    const zip = await JSZip.loadAsync(await saved[0].arrayBuffer());
    expect(Object.keys(zip.files).filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))).toHaveLength(8);
    expect(toastError).not.toHaveBeenCalled();
  });
});
