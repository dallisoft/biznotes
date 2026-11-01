import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SkillReportPanel } from "@/components/skills/skill-report-panel";
import type { SkillReport } from "@/lib/skill-types";

const REPORT: SkillReport = {
  standard: { profile: "legacy_v1", errors: ["Unexpected fields in frontmatter: version."] },
  portability: { warnings: [{ code: "non_ascii_name", message: "name에 한글이 있습니다" }] },
  policy: {
    errors: [],
    warnings: [{ code: "license_restricts_redistribution", message: "재배포 제한" }],
    license: { field: "Proprietary", files: [] },
  },
  capability: {
    runtime_profile: "legacy_instruction_v1",
    warnings: [{ code: "scripts_not_executable", message: "번들 스크립트는 실행되지 않습니다" }],
  },
};

describe("SkillReportPanel", () => {
  it("표준 위반을 거부가 아닌 보고로 보여 준다", () => {
    render(<SkillReportPanel report={REPORT} />);

    expect(screen.getByText("표준 비준수")).toBeInTheDocument();
    expect(screen.getByText(/표준 위반 \(보고만/)).toBeInTheDocument();
    expect(screen.getByText("Unexpected fields in frontmatter: version.")).toBeInTheDocument();
  });

  it("층별 경고를 각각 보여 준다", () => {
    render(<SkillReportPanel report={REPORT} />);

    expect(screen.getByText("name에 한글이 있습니다")).toBeInTheDocument();
    expect(screen.getByText("재배포 제한")).toBeInTheDocument();
    expect(screen.getByText("번들 스크립트는 실행되지 않습니다")).toBeInTheDocument();
    expect(screen.getByText("legacy_instruction_v1")).toBeInTheDocument();
  });

  it("표준을 지킨 패키지는 위반 목록을 그리지 않는다", () => {
    render(
      <SkillReportPanel
        report={{ standard: { profile: "standard_v1", errors: [] }, capability: {} }}
      />,
    );

    expect(screen.getByText("표준 준수")).toBeInTheDocument();
    expect(screen.queryByText(/표준 위반/)).not.toBeInTheDocument();
  });

  it("보고서가 없으면 아무것도 그리지 않는다", () => {
    const { container } = render(<SkillReportPanel report={null} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("가져오기의 루트 폴더 불일치는 경고 배지로만 보인다(거부 아님)", () => {
    render(
      <SkillReportPanel
        report={{
          standard: { profile: "standard_v1", errors: [] },
          root_mismatch: { code: "root_mismatch", message: "루트 폴더가 다릅니다" },
        }}
      />,
    );

    expect(screen.getByText("루트 폴더 이름 다름")).toBeInTheDocument();
  });
});
