import { readFileSync } from "node:fs";
import path from "node:path";
import { render, renderHook, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { DangerZone, DangerZoneItem } from "@/components/layout/danger-zone";
import { StickySaveBar } from "@/components/layout/sticky-save-bar";
import { Toolbar, ToolbarSearch } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { PAGE_SIZE_OPTIONS, usePagination } from "@/hooks/use-pagination";
import { ConfirmDialog } from "./confirm-dialog";
import { EmptyState, FilteredEmptyState } from "./empty-state";
import { ErrorState } from "./error-state";
import { RowActions } from "./row-actions";
import { SegmentedControl } from "./segmented-control";
import { StatCard } from "./stat-card";
import { TablePagination } from "./table-pagination";

// plan §6.7 「터치 기기 — pointer: coarse이면 44px 타깃」·§6.8-8 (P1 리뷰 P1-02).
// jsdom은 미디어 쿼리·레이아웃을 계산하지 않으므로 여기서는 「coarse 변형으로 44px(11 = 2.75rem) 이상」 클래스 계약을
// 고정하고, 그 변형이 실제로 (pointer: coarse) 미디어 쿼리라는 것을 globals.css에서 확인한다. 실브라우저 측정(터치
// 에뮬레이션)은 reports/phase-1의 touch-target 측정 산출물이 맡는다.
const HEIGHT = /(^|\s)coarse:(min-h|h|size)-11(\s|$)/;
const WIDTH = /(^|\s)coarse:(min-w|w|size)-11(\s|$)/;

function expectTouchTarget(el: HTMLElement, { width = true }: { width?: boolean } = {}) {
  const name = el.getAttribute("aria-label") ?? el.textContent ?? el.tagName;
  expect(el.className, `${name}: coarse 높이 44px`).toMatch(HEIGHT);
  if (width) expect(el.className, `${name}: coarse 폭 44px`).toMatch(WIDTH);
}

describe("coarse 변형 정의", () => {
  it("coarse: 변형은 (pointer: coarse) 미디어 쿼리다", () => {
    const css = readFileSync(path.resolve(__dirname, "../../app/globals.css"), "utf8");
    expect(css).toMatch(/@custom-variant coarse \(@media \(pointer: coarse\)\);/);
  });
});

describe("P1 공용 컴포넌트의 터치 타깃(pointer: coarse 44px)", () => {
  function OpenConfirm({ destructive }: { destructive: boolean }) {
    const opener = useRef<HTMLButtonElement>(null);
    return (
      <>
        <button ref={opener}>열기</button>
        <ConfirmDialog
          open
          onOpenChange={() => {}}
          title="‘gemini’ 프로바이더를 삭제할까요?"
          actionLabel="삭제"
          destructive={destructive}
          onConfirm={() => {}}
          returnFocusTo={opener}
        />
      </>
    );
  }

  it.each([true, false])(
    "ConfirmDialog(destructive=%s): 「취소」·확인 버튼 모두 높이·폭 44px 이상이고 초기 포커스는 「취소」다(P1-02)",
    async (destructive) => {
      render(<OpenConfirm destructive={destructive} />);
      const dialog = await screen.findByRole("alertdialog");
      const cancel = within(dialog).getByRole("button", { name: "취소" });
      const confirm = within(dialog).getByRole("button", { name: "삭제" });
      expectTouchTarget(cancel);
      expectTouchTarget(confirm);
      await vi.waitFor(() => expect(cancel).toHaveFocus());
      if (destructive) expect(confirm.className).toMatch(/\bbg-destructive\b/);
    },
  );

  it("RowActions: 주 동작·⋯ 트리거·메뉴 항목(일반·위험)", async () => {
    const user = userEvent.setup();
    render(
      <RowActions
        rowLabel="gemini"
        primary={{ label: "수정", onClick: () => {} }}
        items={[
          { label: "복제", onSelect: () => {} },
          {
            label: "삭제",
            destructive: true,
            confirm: { title: "‘gemini’ 항목을 삭제할까요?", actionLabel: "삭제" },
            onConfirm: () => {},
          },
        ]}
      />,
    );
    expectTouchTarget(screen.getByRole("button", { name: "수정" }));
    const trigger = screen.getByRole("button", { name: "gemini 동작 더보기" });
    expectTouchTarget(trigger);
    await user.click(trigger);
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual(["복제", "삭제"]);
    for (const item of items) expectTouchTarget(item, { width: false }); // 메뉴 폭은 min-w-40(160px)
  });

  it("SegmentedControl: 짧은 라벨(「7일」)의 radio도 높이·폭 44px 이상", () => {
    render(
      <SegmentedControl
        label="기간"
        options={[
          { value: "7", label: "7일" },
          { value: "30", label: "30일" },
        ]}
        value="7"
        onValueChange={() => {}}
      />,
    );
    for (const radio of screen.getAllByRole("radio")) expectTouchTarget(radio);
  });

  it("EmptyState·FilteredEmptyState·ErrorState의 동작 버튼·링크", () => {
    render(
      <>
        <EmptyState title="노트북이 없습니다" action={{ label: "새 노트북", onClick: () => {} }} />
        <FilteredEmptyState onClearFilters={() => {}} />
        <ErrorState onRetry={() => {}} back={{ href: "/skills", label: "스킬 목록으로" }} />
      </>,
    );
    expectTouchTarget(screen.getByRole("button", { name: "새 노트북" }));
    expectTouchTarget(screen.getByRole("button", { name: "필터 지우기" }));
    expectTouchTarget(screen.getByRole("button", { name: "다시 시도" }));
    expectTouchTarget(screen.getByRole("link", { name: "스킬 목록으로" }));
  });

  it("DangerZone 항목 버튼·StickySaveBar 버튼·Toolbar 검색 입력", () => {
    render(
      <>
        <DangerZone>
          <DangerZoneItem
            title="전체 재인덱싱"
            buttonLabel="전체 재인덱싱…"
            confirm={{ title: "전체 재인덱싱할까요?", actionLabel: "재인덱싱" }}
            onConfirm={() => {}}
          />
        </DangerZone>
        <StickySaveBar dirty onSave={() => {}} onReset={() => {}} />
        <Toolbar>
          <ToolbarSearch label="노트북 검색" />
        </Toolbar>
      </>,
    );
    expectTouchTarget(screen.getByRole("button", { name: "전체 재인덱싱…" }));
    expectTouchTarget(screen.getByRole("button", { name: "되돌리기" }));
    expectTouchTarget(screen.getByRole("button", { name: "변경 사항 저장" }));
    const search = screen.getByRole("searchbox", { name: "노트북 검색" });
    expectTouchTarget(search, { width: false });
    expect(search.className).toMatch(/(^|\s)coarse:min-h-11(\s|$)/); // 입력 높이도 명시도와 무관한 min-height
  });

  it("TablePagination: 페이지당 개수·이전/다음", () => {
    const items = Array.from({ length: PAGE_SIZE_OPTIONS[0] + 1 }, (_, i) => i);
    const { result } = renderHook(() => usePagination(items));
    render(<TablePagination pagination={result.current} />);
    const pageSize = screen.getByLabelText("페이지당 개수");
    expectTouchTarget(pageSize, { width: false }); // 폭 4.5rem(72px)
    // SelectTrigger 기본 높이는 data-[size=default]:h-8(클래스+속성 명시도)이라 coarse:h-11(클래스 하나)이 진다 —
    // 실브라우저 터치 측정에서 32px로 남았던 회귀(게이트 3회차 뒤 측정). 명시도와 무관한 min-height로 고정한다.
    expect(pageSize.className).toMatch(/(^|\s)coarse:min-h-11(\s|$)/);
    expectTouchTarget(screen.getByRole("button", { name: "이전 페이지" }));
    expectTouchTarget(screen.getByRole("button", { name: "다음 페이지" }));
  });

  it("StatCard 링크·PageHeader 브레드크럼 링크(텍스트 링크는 높이만)", () => {
    render(
      <>
        <PageHeader title="LLM 게이트웨이" breadcrumb={[{ label: "관리", href: "/admin" }, { label: "LLM 게이트웨이" }]} />
        <StatCard label="검토 대기" value="3" href="/admin/skills" linkLabel="검토하기" />
      </>,
    );
    expectTouchTarget(screen.getByRole("link", { name: "관리" }), { width: false });
    expectTouchTarget(screen.getByRole("link", { name: "검토하기 — 검토 대기" }), { width: false });
  });
});
