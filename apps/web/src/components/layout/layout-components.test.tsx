import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Input } from "@/components/ui/input";
import { DangerZone, DangerZoneItem } from "./danger-zone";
import { FormField, FormSection } from "./form-section";
import { PageContainer } from "./page-container";
import { StickySaveBar } from "./sticky-save-bar";
import { Toolbar, ToolbarSearch } from "./toolbar";

describe("PageContainer", () => {
  it.each([
    ["wide", "max-w-[96rem]"],
    ["default", "max-w-5xl"],
    ["narrow", "max-w-3xl"],
  ] as const)("%s 폭은 %s + 반응형 패딩(16/24/32)이고 main 랜드마크가 아니다", (width, cls) => {
    const { container } = render(<PageContainer width={width}>본문</PageContainer>);
    const el = container.firstElementChild as HTMLElement;
    expect(el.tagName).toBe("DIV");
    expect(el).toHaveAttribute("data-width", width);
    expect(el).toHaveClass("mx-auto", cls, "px-4", "md:px-6", "lg:px-8");
    expect(screen.queryByRole("main")).toBeNull();
  });

  it("기본은 wide, bleed는 폭 제한·패딩이 없다", () => {
    const { container, rerender } = render(<PageContainer>본문</PageContainer>);
    expect(container.firstElementChild).toHaveAttribute("data-width", "wide");
    rerender(<PageContainer width="bleed">본문</PageContainer>);
    const el = container.firstElementChild as HTMLElement;
    expect(el.className).not.toMatch(/max-w-|px-/);
    expect(el).toHaveClass("h-full", "min-h-0");
  });
});

describe("Toolbar", () => {
  it("왼쪽 검색·필터와 오른쪽 end 슬롯을 그리고, 검색 입력은 접근 가능한 이름을 가진다", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Toolbar end={<button>카드</button>}>
        <ToolbarSearch label="노트북 검색" onChange={(e) => onChange(e.target.value)} />
        <button>필터</button>
      </Toolbar>,
    );
    const search = screen.getByRole("searchbox", { name: "노트북 검색" });
    expect(search).toHaveAttribute("placeholder", "노트북 검색");
    await user.type(search, "회의");
    expect(onChange).toHaveBeenLastCalledWith("회의");
    expect(screen.getByRole("button", { name: "카드" }).parentElement).toHaveClass("ml-auto");
  });
});

describe("DangerZone", () => {
  it("「위험 구역」 제목의 섹션이고, 항목 버튼은 확인을 거쳐야 실행된다", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <DangerZone>
        <DangerZoneItem
          title="전체 재인덱싱"
          description="모든 소스의 임베딩을 다시 만듭니다."
          buttonLabel="전체 재인덱싱"
          confirm={{ title: "전체 재인덱싱할까요?", description: "시간이 오래 걸립니다.", actionLabel: "재인덱싱" }}
          onConfirm={onConfirm}
        />
      </DangerZone>,
    );
    const region = screen.getByRole("region", { name: "위험 구역" });
    expect(region).toHaveClass("border-destructive/40");
    const button = within(region).getByRole("button", { name: "전체 재인덱싱" });
    expect(button).toHaveClass("text-destructive-text");
    await user.click(button);
    expect(onConfirm).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog", { name: "전체 재인덱싱할까요?" });
    await user.click(within(dialog).getByRole("button", { name: "재인덱싱" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(button).toHaveFocus());
  });
});

describe("FormSection · FormField", () => {
  it("섹션 제목으로 이름 붙은 영역이고, 필드는 라벨·단위·범위·도움말을 연결한다", () => {
    render(
      <FormSection title="생성" description="답변 생성 파라미터">
        <FormField label="최대 토큰" unit="토큰" range="1~8192" help="한 번에 만들 최대 길이">
          {(field) => <Input type="number" defaultValue={1024} {...field} />}
        </FormField>
      </FormSection>,
    );
    expect(screen.getByRole("region", { name: "생성" })).toBeInTheDocument();
    const input = screen.getByRole("spinbutton", { name: "최대 토큰" });
    expect(input).toHaveAccessibleDescription("한 번에 만들 최대 길이 범위 1~8192");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(screen.getByText("토큰")).toBeInTheDocument();
  });

  it("제목은 기본 h2이고, headingLevel={3}이면 h3로 내려 페이지 섹션(h2) 아래에 묶을 수 있다", () => {
    const { rerender } = render(
      <FormSection title="청킹">
        <p>필드</p>
      </FormSection>,
    );
    expect(screen.getByRole("heading", { name: "청킹", level: 2 })).toBeInTheDocument();
    rerender(
      <FormSection title="청킹" headingLevel={3}>
        <p>필드</p>
      </FormSection>,
    );
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
    expect(screen.getByRole("heading", { name: "청킹", level: 3 })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "청킹" })).toBeInTheDocument();
  });

  it("검증 오류가 있으면 aria-invalid + 오류 문구를 설명에 연결한다", () => {
    render(
      <FormField label="온도" range="0~1" error="0과 1 사이 값을 입력하세요">
        {(field) => <Input {...field} defaultValue="3" />}
      </FormField>,
    );
    const input = screen.getByRole("textbox", { name: "온도" });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("범위 0~1 0과 1 사이 값을 입력하세요");
  });

  it("도움말·범위·오류가 없으면 설명 연결도 없다", () => {
    render(<FormField label="이름">{(field) => <Input {...field} />}</FormField>);
    expect(screen.getByRole("textbox", { name: "이름" })).not.toHaveAttribute("aria-describedby");
  });
});

describe("StickySaveBar", () => {
  function Form() {
    const [value, setValue] = useState("a");
    const [saved, setSaved] = useState("a");
    const [saving, setSaving] = useState(false);
    return (
      <>
        <input aria-label="값" value={value} onChange={(e) => setValue(e.target.value)} />
        <button onClick={() => setSaving((s) => !s)}>저장 상태 전환</button>
        <StickySaveBar
          dirty={value !== saved}
          saving={saving}
          onSave={() => setSaved(value)}
          onReset={() => setValue(saved)}
        />
      </>
    );
  }

  it("바뀐 것이 없으면 보이지 않고, 바뀌면 안내 + 되돌리기 + 변경 사항 저장을 보인다", async () => {
    const user = userEvent.setup();
    render(<Form />);
    expect(screen.queryByRole("region", { name: "저장되지 않은 변경 사항" })).toBeNull();
    await user.type(screen.getByLabelText("값"), "b");
    const bar = screen.getByRole("region", { name: "저장되지 않은 변경 사항" });
    expect(within(bar).getByRole("status")).toHaveTextContent("저장되지 않은 변경 사항이 있습니다");
    await user.click(within(bar).getByRole("button", { name: "되돌리기" }));
    expect(screen.getByLabelText("값")).toHaveValue("a");
    expect(screen.queryByRole("region", { name: "저장되지 않은 변경 사항" })).toBeNull();

    await user.type(screen.getByLabelText("값"), "c");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    expect(screen.queryByRole("region", { name: "저장되지 않은 변경 사항" })).toBeNull();
  });

  it("저장 중에는 두 버튼이 비활성이고 문구가 「저장 중…」이다", async () => {
    const user = userEvent.setup();
    render(<Form />);
    await user.type(screen.getByLabelText("값"), "b");
    await user.click(screen.getByRole("button", { name: "저장 상태 전환" }));
    const bar = screen.getByRole("region", { name: "저장되지 않은 변경 사항" });
    expect(within(bar).getByRole("button", { name: "되돌리기" })).toBeDisabled();
    expect(within(bar).getByRole("button", { name: "저장 중…" })).toBeDisabled();
  });
});
