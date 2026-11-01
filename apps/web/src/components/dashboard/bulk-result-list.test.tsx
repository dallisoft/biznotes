import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BULK_MESSAGES, type BulkItemResult, STOP_NOTICES } from "@/lib/notebook-bulk";
import { BulkResultList } from "./bulk-result-list";

// 결과 목록(삭제·공유 공용) — 항목별 상태 표시와 확인 불가 구분.

const results: BulkItemResult[] = [
  { id: "a", title: "A", status: "succeeded", message: BULK_MESSAGES.deleted, unconfirmed: false },
  { id: "b", title: "B", status: "failed", message: BULK_MESSAGES.forbidden, unconfirmed: false },
  { id: "c", title: "C", status: "failed", message: BULK_MESSAGES.unconfirmed, unconfirmed: true },
  { id: "d", title: "D", status: "not_run", message: BULK_MESSAGES.notRun, unconfirmed: false },
  { id: "e", title: "E", status: "skipped", message: "이미 편집 가능 권한으로 공유됨", unconfirmed: false },
];

describe("BulkResultList", () => {
  it("항목마다 상태 라벨·제목·사유를 입력 순서대로 보이고 응답 손실은 「확인 불가」로 구분한다", () => {
    render(<BulkResultList results={results} summary="요약" notice="중지했습니다" />);
    expect(screen.getByTestId("bulk-result-summary")).toHaveTextContent("요약");
    expect(screen.getByRole("alert")).toHaveTextContent("중지했습니다");
    const items = within(screen.getByRole("list", { name: "항목별 결과" })).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      `완료A${BULK_MESSAGES.deleted}`,
      `실패B${BULK_MESSAGES.forbidden}`,
      `확인 불가C${BULK_MESSAGES.unconfirmed}`,
      `미실행D${BULK_MESSAGES.notRun}`,
      "건너뜀E이미 편집 가능 권한으로 공유됨",
    ]);
    expect(items[2]).toHaveAttribute("data-unconfirmed", "true");
    expect(items[1]).not.toHaveAttribute("data-unconfirmed");
  });

  it("그룹 대상 중단 안내는 alert 한 줄이고, 항목에는 상태·사유만 있다 — 보상 경고 줄 없음(core-v0.20.0 §5.3·§5.8)", () => {
    render(
      <BulkResultList
        results={[results[3], { ...results[3], id: "d2", title: "D2" }]}
        summary="공유 0 · 건너뜀 0 · 실패 0 · 확인 불가 0 · 미실행 2"
        notice={STOP_NOTICES.target_group_missing}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(STOP_NOTICES.target_group_missing);
    const items = screen.getAllByTestId("bulk-result-item");
    expect(items.map((li) => li.textContent)).toEqual([`미실행D${BULK_MESSAGES.notRun}`, `미실행D2${BULK_MESSAGES.notRun}`]);
    expect(screen.queryByTestId("bulk-result-warning")).not.toBeInTheDocument();
    expect(items[0]).not.toHaveAttribute("data-warning");
  });

  it("안내가 없으면 alert를 그리지 않는다", () => {
    render(<BulkResultList results={results.slice(0, 1)} summary="삭제 1" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
