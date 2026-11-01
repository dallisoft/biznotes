import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceDetailInline } from "@/components/sources/source-detail-inline";
import { ApiError } from "@/lib/api-client";
import type { SourceDetail } from "@/lib/source-types";

// API 경계만 mock(AC-3.7) — 실제 useSourceDetail·useSourceGuide 훅이 이 api를 부른다.
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

function makeDetail(overrides: Partial<SourceDetail>): SourceDetail {
  return {
    id: "s1",
    notebook_id: "nb1",
    type: "file",
    title: "스캔 문서.pdf",
    origin_url: null,
    mime_type: "application/pdf",
    size_bytes: 1024,
    status: "ready",
    error_message: null,
    token_count: 100,
    chunk_count: 3,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    parsed_preview: "본문",
    summary: null,
    keywords: null,
    ...overrides,
  };
}

async function renderDetail(detail: SourceDetail, onBack = vi.fn(), highlightQuote?: string) {
  api.get.mockImplementation((path: string) => {
    if (path === `/api/v1/sources/${detail.id}`) return Promise.resolve(detail);
    if (path === `/api/v1/sources/${detail.id}/guide`) {
      return Promise.reject(new ApiError(500, "가이드 실패"));
    }
    return Promise.reject(new Error(`예상 밖 GET ${path}`));
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SourceDetailInline source={detail} onBack={onBack} highlightQuote={highlightQuote} />
    </QueryClientProvider>,
  );
  // 머리글(제목)은 목록의 소스로 바로 그리므로 상세 본문이 들어온 뒤(마크다운 영역)를 기다린다
  await screen.findByText("마크다운");
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("SourceDetailInline — 패널 전체를 차지하는 상세 화면의 머리글", () => {
  it("제목은 머리글 h2에 한 번만 나오고 부제에 상태·청크·토큰이, 본문에 원본 링크·마크다운이 있다", async () => {
    await renderDetail(
      makeDetail({ type: "web", title: "커피 추출 가이드", origin_url: "https://example.com/coffee", token_count: 1234, chunk_count: 7 }),
    );
    const view = screen.getByTestId("source-detail-view");
    expect(within(view).getByRole("heading", { level: 2, name: "커피 추출 가이드" })).toBeInTheDocument();
    expect(within(view).getAllByText("커피 추출 가이드")).toHaveLength(1);
    expect(within(view).queryByRole("heading", { level: 3 })).toBeNull();
    expect(view).toHaveTextContent("준비됨 · 청크 7개 · 토큰 1,234");
    expect(within(view).getByRole("link", { name: "원본 링크" })).toHaveAttribute(
      "href",
      "https://example.com/coffee",
    );
    expect(within(view).getByText("본문")).toBeInTheDocument();
  });

  it("열리면 제목으로 포커스가 가고, 「목록」을 누르면 onBack을 부른다", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    await renderDetail(makeDetail({}), onBack);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 2, name: "스캔 문서.pdf" })).toHaveFocus(),
    );
    await user.click(screen.getByTestId("source-detail-back"));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("상세를 받기 전에도 머리글은 목록의 소스로 바로 보이고, 본문은 불러오는 중이다", async () => {
    api.get.mockImplementation(() => new Promise(() => {}));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SourceDetailInline source={makeDetail({ status: "parsing" })} onBack={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "스캔 문서.pdf" })).toBeInTheDocument();
    expect(screen.getByTestId("source-detail-view")).toHaveTextContent("파싱 중 · 청크 3개");
    expect(screen.queryByText("마크다운")).toBeNull();
  });

  it("상세를 불러오지 못하면 머리글은 남고 본문에 오류를 보인다", async () => {
    api.get.mockRejectedValue(new ApiError(500, "서버 오류"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SourceDetailInline source={makeDetail({})} onBack={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/불러오지 못했습니다: 서버 오류/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "스캔 문서.pdf" })).toBeInTheDocument();
  });
});

describe("SourceDetailInline — 문서 제목 보조 줄 (v7 AC-25, AC-3.6)", () => {
  it("document_title이 있고 title과 다르면 보조 줄(2줄 제한)로 보인다", async () => {
    await renderDetail(makeDetail({ title: "report.hwp", document_title: "2026 업무 계획" }));
    const line = screen.getByTestId("source-document-title");
    expect(line).toHaveTextContent("2026 업무 계획");
    expect(line).toHaveClass("line-clamp-2");
  });

  it.each([
    ["없음(null)", { document_title: null }],
    ["필드 부재(구 API)", {}],
    ["title과 같음", { title: "Example Domain", document_title: "Example Domain" }],
  ])("%s이면 보조 줄이 없다", async (_label, overrides) => {
    await renderDetail(makeDetail(overrides));
    expect(screen.queryByTestId("source-document-title")).not.toBeInTheDocument();
  });

  it("태그 문자열은 텍스트 그대로 보인다(T-6)", async () => {
    await renderDetail(makeDetail({ document_title: "<b>x</b><img src=x onerror=alert(1)>" }));
    const line = screen.getByTestId("source-document-title");
    expect(line).toHaveTextContent("<b>x</b><img src=x onerror=alert(1)>");
    expect(line.querySelector("b, img")).toBeNull();
  });
});

describe("SourceDetailInline — 인제스트 경고 (v7 AC-25, AC-3.6)", () => {
  it.each([
    ["null(기록 없음)", null],
    ["[](경고 없음)", []],
  ])("warnings가 %s이면 경고 영역이 없다", async (_label, warnings) => {
    await renderDetail(makeDetail({ warnings }));
    expect(screen.queryByTestId("source-warnings")).not.toBeInTheDocument();
  });

  it("코드별 문구·params·모르는 코드 fallback·보안 신호 주의 스타일", async () => {
    await renderDetail(
      makeDetail({
        warnings: [
          { code: "text_truncated", params: { omitted: 7 } },
          { code: "injection_signals", params: { count: 2, confidence: "medium" } },
          { code: "future_code", params: {} },
        ],
      }),
    );
    const items = within(screen.getByTestId("source-warnings")).getAllByTestId("source-warning");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("본문이 처리 상한에 닿아 일부가 잘렸습니다(생략 7개).");
    expect(items[0]).toHaveAttribute("data-security", "false");
    expect(items[1]).toHaveTextContent("프롬프트 인젝션으로 보이는 문구가 있습니다(2건, 신뢰도 중간).");
    expect(items[1]).toHaveAttribute("data-security", "true");
    expect(items[1].className).toContain("bg-warning/10");
    expect(items[2]).toHaveTextContent("알 수 없는 인제스트 경고(future_code)");
    expect(items[2]).toHaveAttribute("data-security", "false");
  });

  it("문자열 params의 태그는 텍스트 그대로 보인다(T-6)", async () => {
    await renderDetail(
      makeDetail({ warnings: [{ code: "threat_findings", params: { severity: "<b>x</b>", count: 1 } }] }),
    );
    const item = screen.getByTestId("source-warning");
    expect(item).toHaveTextContent("<b>x</b>");
    expect(item.querySelector("b")).toBeNull();
  });
});

describe("SourceDetailInline — 임베딩 지연 안내 (core-v0.12.0-upgrade AC-2.6)", () => {
  it("pending 소스의 지연 안내는 오류 스타일이 아니다", async () => {
    await renderDetail(
      makeDetail({ status: "pending", error_message: "임베딩 지연 — 60초 뒤 다시 시도합니다" }),
    );
    const message = screen.getByTestId("source-error-message");
    expect(message).toHaveTextContent("임베딩 지연");
    expect(message).not.toHaveClass("text-destructive");
  });

  it("failed 소스의 오류는 오류 스타일로 보인다", async () => {
    await renderDetail(makeDetail({ status: "failed", error_message: "파싱 실패" }));
    expect(screen.getByTestId("source-error-message")).toHaveClass("text-destructive");
  });
});

describe("SourceDetailInline — 재인덱싱 안내 (core-v0.16.0-upgrade AC-1.4, 코어 #230)", () => {
  it("v0.16.0 ready 행: 안내는 warnings로만 오고 error_message 영역은 없다", async () => {
    await renderDetail(
      makeDetail({
        status: "ready",
        error_message: null,
        warnings: [{ code: "reindex_kept_previous", params: { reason: "embedding_transient" } }],
      }),
    );
    const items = within(screen.getByTestId("source-warnings")).getAllByTestId("source-warning");
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent("재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 임베딩 서버 일시 오류).");
    expect(items[0]).toHaveAttribute("data-security", "false");
    expect(screen.queryByTestId("source-error-message")).not.toBeInTheDocument();
  });

  it("전환기: v0.16.0 전에 쓴 ready 행의 옛 재인덱싱 문구는 계속 muted로 보인다", async () => {
    await renderDetail(
      makeDetail({
        status: "ready",
        error_message: "재인덱싱 실패 — 이전 색인을 유지합니다: 임베딩 서버 일시 오류",
        warnings: [],
      }),
    );
    const message = screen.getByTestId("source-error-message");
    expect(message).toHaveTextContent("재인덱싱 실패 — 이전 색인을 유지합니다");
    expect(message).toHaveClass("text-muted-foreground");
    expect(message).not.toHaveClass("text-destructive");
    expect(screen.queryByTestId("source-warnings")).not.toBeInTheDocument();
  });
});

describe("SourceDetailInline — 인용 구간 하이라이트(ui-ux-improvements ②)", () => {
  const PREVIEW = "# 취업규칙\n\n## 제5장 징계\n\n징계의 종류는 견책, 감봉, 정직, 해고로 한다.\n\n## 제6장 휴가\n\n연차는 15일이다.";

  it("인용과 맞는 블록을 표시하고 화면 가운데로 스크롤한다", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    await renderDetail(makeDetail({ parsed_preview: PREVIEW }), vi.fn(), "## 제5장 징계\n\n징계의 종류는 견책, 감봉, 정직, 해고로 한다.");
    const markdown = screen.getByTestId("markdown-content");
    await waitFor(() =>
      expect(Array.from(markdown.querySelectorAll("[data-cited]")).map((e) => e.textContent)).toEqual([
        "제5장 징계",
        "징계의 종류는 견책, 감봉, 정직, 해고로 한다.",
      ]),
    );
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
    expect(screen.queryByText(/인용 위치를 찾지 못했습니다/)).not.toBeInTheDocument();
  });

  it("원문 미리보기에서 찾지 못하면 그 사실을 알린다", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    await renderDetail(makeDetail({ parsed_preview: PREVIEW }), vi.fn(), "미리보기에 없는 아주 다른 내용의 인용 문장입니다.");
    expect(await screen.findByRole("status")).toHaveTextContent("인용 위치를 찾지 못했습니다");
    expect(screen.getByTestId("markdown-content").querySelector("[data-cited]")).toBeNull();
  });

  it("인용 없이 열면 표시도 안내도 없다", async () => {
    await renderDetail(makeDetail({ parsed_preview: PREVIEW }));
    expect(screen.getByTestId("markdown-content").querySelector("[data-cited]")).toBeNull();
    expect(screen.queryByText(/인용 위치를 찾지 못했습니다/)).not.toBeInTheDocument();
  });
});
