import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InfographicViewer } from "@/components/agents/infographic-viewer";

const DOC = {
  title: "녹차 시장 개요",
  subtitle: "2025년 기준",
  key_stats: [
    { label: "시장 규모", value: "1.2조 원" },
    { label: "성장률", value: "7.5%", note: "전년 대비" },
  ],
  sections: [
    { heading: "주요 산지", points: ["보성", "하동"] },
    { heading: "소비 동향", points: ["티백 비중 증가"] },
  ],
  charts: [
    {
      title: "산지별 생산량",
      unit: "톤",
      data: [
        { label: "보성", value: 1200 },
        { label: "하동", value: 600 },
      ],
    },
  ],
};

describe("InfographicViewer", () => {
  it("제목·부제·핵심 수치·섹션을 렌더한다", () => {
    render(<InfographicViewer content={JSON.stringify(DOC)} />);
    expect(screen.getByTestId("infographic-viewer")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "녹차 시장 개요" })).toBeInTheDocument();
    expect(screen.getByText("2025년 기준")).toBeInTheDocument();

    const stats = screen.getAllByTestId("infographic-stat");
    expect(stats).toHaveLength(2);
    expect(within(stats[0]).getByText("1.2조 원")).toBeInTheDocument();
    expect(within(stats[0]).getByText("시장 규모")).toBeInTheDocument();
    expect(within(stats[1]).getByText("전년 대비")).toBeInTheDocument();

    expect(screen.getByRole("heading", { level: 3, name: "주요 산지" })).toBeInTheDocument();
    expect(screen.getByText("티백 비중 증가")).toBeInTheDocument();
  });

  it("차트 막대는 값 비율로 너비를 잡고 라벨·값을 읽을 수 있게 둔다", () => {
    render(<InfographicViewer content={JSON.stringify(DOC)} />);
    const chart = screen.getByRole("list", { name: "산지별 생산량" });
    const bars = within(chart).getAllByTestId("infographic-bar");
    expect(bars).toHaveLength(2);
    expect(bars[0]).toHaveStyle({ width: "100%" });
    expect(bars[1]).toHaveStyle({ width: "50%" });
    expect(bars[0]).toHaveAttribute("aria-hidden", "true");
    const rows = within(chart).getAllByRole("listitem");
    expect(rows[0]).toHaveAccessibleName("보성: 1,200톤");
  });

  it("항목이 모두 비면 제목과 빈 안내를 보인다", () => {
    render(
      <InfographicViewer
        content={JSON.stringify({ title: "빈 인포그래픽", key_stats: [], sections: [] })}
      />,
    );
    expect(screen.getByRole("heading", { name: "빈 인포그래픽" })).toBeInTheDocument();
    expect(screen.getByTestId("infographic-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("infographic-stat")).not.toBeInTheDocument();
  });

  it("```json 펜스로 감싼 본문도 렌더한다", () => {
    render(<InfographicViewer content={"```json\n" + JSON.stringify(DOC) + "\n```"} />);
    expect(screen.getByRole("heading", { level: 2, name: "녹차 시장 개요" })).toBeInTheDocument();
  });

  it("형식이 맞지 않으면 안내와 함께 원문 JSON으로 대체한다", () => {
    const content = JSON.stringify({ root: { id: "r" } });
    render(<InfographicViewer content={content} />);
    expect(screen.queryByTestId("infographic-viewer")).not.toBeInTheDocument();
    expect(screen.getByTestId("infographic-fallback")).toHaveTextContent(/형식이 아니어서/);
    expect(screen.getByTestId("infographic-raw")).toHaveTextContent('"root"');
  });

  it("깨진 JSON은 원문 그대로 보인다", () => {
    render(<InfographicViewer content="JSON 아님 {" />);
    expect(screen.getByTestId("infographic-raw")).toHaveTextContent("JSON 아님 {");
  });
});
