import { describe, expect, it } from "vitest";
import { locateQuote } from "@/lib/quote-locate";

function dom(html: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = html;
  return root;
}

describe("locateQuote — 인용 청크를 렌더된 원문 블록에서 찾는다(ui-ux-improvements ②)", () => {
  it("마크다운 기호·줄바꿈·공백 차이를 무시하고 해당 블록들을 돌려준다", () => {
    const root = dom(
      "<h2>제3장 근로시간</h2><p>근로시간은 1일 8시간,\n1주 40시간으로 한다.</p><p>휴게시간은 별도다.</p><p>무관한 문단</p>",
    );
    const quote = "## 제3장 근로시간\n\n근로시간은 **1일 8시간**, 1주 40시간으로 한다.\n\n휴게시간은 별도다.";
    const blocks = locateQuote(root, quote);
    expect(blocks?.map((b) => b.textContent)).toEqual([
      "제3장 근로시간",
      "근로시간은 1일 8시간,\n1주 40시간으로 한다.",
      "휴게시간은 별도다.",
    ]);
  });

  it("인용 앞부분이 원문에 없어도(청크 접두가 다르면) 뒤쪽 조각으로 찾는다", () => {
    const root = dom(
      "<p>연차 유급휴가는 1년간 80퍼센트 이상 출근한 근로자에게 15일을 부여한다. 계속 근로 연수에 따라 가산한다.</p>",
    );
    const quote =
      "[합성 제목 접두 — 원문에 없음 아주 길게 이어지는 문장 조각입니다 정말로]\n연차 유급휴가는 1년간 80퍼센트 이상 출근한 근로자에게 15일을 부여한다. 계속 근로 연수에 따라 가산한다.";
    const blocks = locateQuote(root, quote);
    expect(blocks).not.toBeNull();
    expect(blocks?.[0].tagName).toBe("P");
  });

  it("표 셀은 셀 단위로 돌려준다", () => {
    const root = dom("<table><tbody><tr><td>견책</td><td>시말서 제출</td></tr></tbody></table>");
    const blocks = locateQuote(root, "| 견책 | 시말서 제출 |");
    expect(blocks?.map((b) => b.tagName)).toEqual(["TD", "TD"]);
  });

  it("원문에 없으면 null", () => {
    const root = dom("<p>전혀 다른 내용의 문서입니다.</p>");
    expect(locateQuote(root, "징계의 종류는 견책, 감봉, 정직, 해고로 한다.")).toBeNull();
  });

  it("너무 짧은 인용은 오탐을 막기 위해 찾지 않는다", () => {
    const root = dom("<p>가나다</p>");
    expect(locateQuote(root, "가나")).toBeNull();
  });
});
