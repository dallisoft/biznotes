import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useSourceFocus } from "@/stores/source-focus";
import { FlashcardViewer } from "@/components/agents/flashcard-viewer";

const CARDS = JSON.stringify([
  {
    id: "c1",
    question: "녹차 우림 온도는?",
    answer: "70~80도",
    citation: {
      chunk_id: "chunk1",
      quote: "녹차는 70~80도의 물에서 2분간 우린다.",
      source_id: "s1",
      source_title: "녹차 가이드",
    },
  },
  { id: "c2", question: "홍차 우림 시간은?", answer: "3~5분" },
]);

describe("FlashcardViewer", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("파싱 불가능한 콘텐츠는 에러 메시지를 표시한다", () => {
    render(<FlashcardViewer content="JSON 아님" runId="r1" artifactName="flashcards.json" />);
    expect(screen.getByText(/해석하지 못했습니다/)).toBeInTheDocument();
  });

  it("첫 카드의 질문을 표시하고 클릭하면 답을 보여준다", () => {
    render(<FlashcardViewer content={CARDS} runId="r1" artifactName="flashcards.json" />);
    expect(screen.getByText("녹차 우림 온도는?")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("flashcard-card"));
    expect(screen.getByText("70~80도")).toBeInTheDocument();
  });

  it("출처 링크 클릭 시 소스 포커스를 요청한다", () => {
    useSourceFocus.setState({ requestedSourceId: null });
    render(<FlashcardViewer content={CARDS} runId="r1" artifactName="flashcards.json" />);
    fireEvent.click(screen.getByTestId("flashcard-card")); // 답 보기
    fireEvent.click(screen.getByTestId("flashcard-source-link"));
    expect(useSourceFocus.getState().requestedSourceId).toBe("s1");
  });

  it("알았음/몰랐음 클릭 시 localStorage에 저장되고 다음 카드로 이동한다", () => {
    render(<FlashcardViewer content={CARDS} runId="r1" artifactName="flashcards.json" />);
    fireEvent.click(screen.getByTestId("flashcard-mark-known"));
    expect(screen.getByText("홍차 우림 시간은?")).toBeInTheDocument();
    expect(screen.getByTestId("flashcard-known-count")).toHaveTextContent("학습 완료 1장");
    const stored = JSON.parse(localStorage.getItem("rn:flashcards:r1:flashcards.json")!);
    expect(stored.c1).toBe("known");
  });

  it("초기화 버튼은 학습 상태를 모두 지운다", () => {
    render(<FlashcardViewer content={CARDS} runId="r1" artifactName="flashcards.json" />);
    fireEvent.click(screen.getByTestId("flashcard-mark-known"));
    fireEvent.click(screen.getByTestId("flashcard-reset"));
    expect(localStorage.getItem("rn:flashcards:r1:flashcards.json")).toBeNull();
    expect(screen.getByTestId("flashcard-known-count")).toHaveTextContent("학습 완료 0장");
  });
});
