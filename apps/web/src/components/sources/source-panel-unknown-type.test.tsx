import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SourcePanel } from "@/components/sources/source-panel";
import type { Source } from "@/lib/source-types";
import { sourceTypeIcon, sourceTypeLucide } from "@/lib/source-types";
import { triggerLabel } from "@/lib/agent-types";

// 프론트 Source["type"] 유니온은 지금 백엔드가 아는 종류만 안다. 옛 판이 남긴 행이나 새 판이 추가한
// 종류처럼 유니온 밖 값이 런타임에 올 수 있다. 그 상황을 재현하려면 타입 단언이 불가피하다.
const UNKNOWN_SOURCE = {
  id: "s-legacy",
  notebook_id: "nb1",
  type: "legacy_kind",
  title: "옛 종류 소스",
  origin_url: null,
  mime_type: null,
  size_bytes: null,
  status: "ready",
  error_message: null,
  token_count: 10,
  chunk_count: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
} as unknown as Source;

vi.mock("@/components/sources/add-source-dialog", () => ({
  AddSourceDialog: () => <div data-testid="add-source-dialog" />,
}));
vi.mock("@/components/sources/source-detail-inline", () => ({
  SourceDetailInline: () => <div data-testid="source-detail-inline" />,
}));
vi.mock("@/hooks/use-sources", () => ({
  useSources: () => ({ data: [UNKNOWN_SOURCE], isPending: false }),
  useDeleteSource: () => ({ mutate: vi.fn() }),
  useRetrySource: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("미지의 소스 타입 방어 (재리뷰 NEW-01)", () => {
  it("유니온 밖 소스 타입이 와도 소스 패널이 크래시하지 않는다", () => {
    // 수정 전: TYPE_LUCIDE[미지의 값] === undefined → <TypeIcon/> 렌더 시 React가 throw
    expect(() => render(<SourcePanel notebookId="nb1" canWrite={false} />)).not.toThrow();
    expect(screen.getByTestId("source-list")).toBeInTheDocument();
    expect(screen.getByTitle("옛 종류 소스")).toBeInTheDocument();
  });

  it("sourceTypeLucide는 미지의 타입에도 항상 컴포넌트를 반환한다", () => {
    for (const t of ["file", "web", "text", "legacy_kind", "완전히_모르는_타입"]) {
      expect(sourceTypeLucide(t)).toBeDefined();
    }
  });

  it("sourceTypeIcon은 미지의 타입에도 항상 문자열을 반환한다", () => {
    expect(sourceTypeIcon("legacy_kind")).toBeTruthy();
    expect(sourceTypeIcon("완전히_모르는_타입")).toBeTruthy();
  });
});

describe("트리거 라벨 방어 (재리뷰 NEW-01)", () => {
  it("정본 TRIGGER_SOURCES 전체에 라벨이 있다", () => {
    // biznotes_core/models/agent.py TRIGGER_SOURCES
    expect(triggerLabel("manual")).toBe("수동");
    expect(triggerLabel("chat")).toBe("채팅");
  });

  it("미지의 트리거는 원문을 그대로 반환한다(빈 문자열 아님)", () => {
    expect(triggerLabel("mystery")).toBe("mystery");
  });
});
