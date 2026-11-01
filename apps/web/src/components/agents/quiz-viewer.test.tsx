import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useSourceFocus } from "@/stores/source-focus";
import { QuizViewer } from "@/components/agents/quiz-viewer";

const QUIZ = JSON.stringify([
  {
    id: "q1",
    question: "녹차 우림 온도는?",
    choices: ["60도", "70~80도", "90도", "100도"],
    answer_index: 1,
    citation: {
      chunk_id: "chunk1",
      quote: "녹차는 70~80도의 물에서 2분간 우린다.",
      source_id: "s1",
      source_title: "녹차 가이드",
    },
  },
]);

describe("QuizViewer", () => {
  it("파싱 불가능한 콘텐츠는 에러 메시지를 표시한다", () => {
    render(<QuizViewer content="JSON 아님" />);
    expect(screen.getByText(/해석하지 못했습니다/)).toBeInTheDocument();
  });

  it("문항과 보기를 렌더한다", () => {
    render(<QuizViewer content={QUIZ} />);
    expect(screen.getByText(/녹차 우림 온도는\?/)).toBeInTheDocument();
    expect(screen.getAllByTestId("quiz-choice")).toHaveLength(4);
  });

  it("정답 선택 시 정답 배지를 표시한다", () => {
    render(<QuizViewer content={QUIZ} />);
    const choices = screen.getAllByTestId("quiz-choice");
    fireEvent.click(choices[1]); // "70~80도" = 정답
    expect(screen.getByTestId("quiz-result-badge")).toHaveTextContent("정답");
  });

  it("오답 선택 시 오답 배지를 표시한다", () => {
    render(<QuizViewer content={QUIZ} />);
    const choices = screen.getAllByTestId("quiz-choice");
    fireEvent.click(choices[0]); // "60도" = 오답
    expect(screen.getByTestId("quiz-result-badge")).toHaveTextContent("오답");
  });

  it("채점 후에는 보기를 다시 선택할 수 없다", () => {
    render(<QuizViewer content={QUIZ} />);
    const choices = screen.getAllByTestId("quiz-choice");
    fireEvent.click(choices[0]);
    for (const choice of screen.getAllByTestId("quiz-choice")) {
      expect(choice).toBeDisabled();
    }
  });

  it("근거 보기 클릭 시 인용 발췌와 출처를 표시한다", () => {
    render(<QuizViewer content={QUIZ} />);
    fireEvent.click(screen.getAllByTestId("quiz-choice")[1]);
    fireEvent.click(screen.getByTestId("quiz-citation-trigger"));
    expect(screen.getByTestId("quiz-citation-popover")).toHaveTextContent(
      "녹차는 70~80도의 물에서 2분간 우린다.",
    );
  });

  it("출처 클릭 시 소스 포커스를 요청한다", () => {
    useSourceFocus.setState({ requestedSourceId: null });
    render(<QuizViewer content={QUIZ} />);
    fireEvent.click(screen.getAllByTestId("quiz-choice")[1]);
    fireEvent.click(screen.getByTestId("quiz-citation-trigger"));
    fireEvent.click(screen.getByText(/출처: 녹차 가이드/));
    expect(useSourceFocus.getState().requestedSourceId).toBe("s1");
  });
});
