import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SegmentedControl } from "./segmented-control";

const RANGES = [
  { value: "7", label: "7일" },
  { value: "14", label: "14일" },
  { value: "30", label: "30일", testId: "range-30" },
] as const;

function Controlled({ onChange }: { onChange?: (v: string) => void }) {
  const [value, setValue] = useState<"7" | "14" | "30">("7");
  return (
    <SegmentedControl
      label="기간"
      options={RANGES}
      value={value}
      onValueChange={(v) => {
        onChange?.(v);
        setValue(v);
      }}
    />
  );
}

describe("SegmentedControl", () => {
  it("radiogroup + radio(aria-checked)로 그리고 줄바꿈 금지 클래스를 쓴다", () => {
    render(<Controlled />);
    const group = screen.getByRole("radiogroup", { name: "기간" });
    expect(group).toHaveClass("whitespace-nowrap");
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.textContent)).toEqual(["7일", "14일", "30일"]);
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false", "false"]);
    expect(screen.getByTestId("range-30")).toBe(radios[2]);
  });

  it("화살표 키로 이동하면 선택이 바뀌고 포커스가 따라간다(끝에서 처음으로 순환)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const [r7, r14, r30] = screen.getAllByRole("radio");
    await user.click(r7);
    expect(r7).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(r14).toHaveFocus());
    expect(r14).toHaveAttribute("aria-checked", "true");
    expect(r7).toHaveAttribute("aria-checked", "false");
    expect(onChange).toHaveBeenLastCalledWith("14");

    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(r30).toHaveFocus());
    expect(onChange).toHaveBeenLastCalledWith("30");

    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(r7).toHaveFocus());
    expect(r7).toHaveAttribute("aria-checked", "true");

    await user.keyboard("{ArrowLeft}");
    await waitFor(() => expect(r30).toHaveFocus());
    expect(r30).toHaveAttribute("aria-checked", "true");
  });

  it("Tab으로 들어오면 선택된 항목 하나만 탭 정지점이다(roving tabindex)", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button>앞</button>
        <Controlled />
        <button>뒤</button>
      </>,
    );
    await user.click(screen.getByRole("button", { name: "앞" }));
    await user.tab();
    expect(screen.getByRole("radio", { name: "7일" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "뒤" })).toHaveFocus();
  });

  it("클릭으로 선택한다", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    await user.click(screen.getByRole("radio", { name: "30일" }));
    expect(onChange).toHaveBeenCalledWith("30");
    expect(screen.getByRole("radio", { name: "30일" })).toHaveAttribute("aria-checked", "true");
  });

  it("iconOnlyOnNarrow면 아이콘이 있는 항목만 좁은 화면에서 라벨을 시각적으로 숨기고, 이름·글자는 그대로다(P3 IR-H5)", () => {
    const options = [
      { value: "card", label: "카드", icon: <svg data-testid="i-card" /> },
      { value: "plain", label: "글자만" },
    ] as const;
    const { rerender } = render(
      <SegmentedControl label="보기" options={options} value="card" onValueChange={() => {}} iconOnlyOnNarrow />,
    );
    const [card, plain] = screen.getAllByRole("radio");
    expect(card).toHaveAccessibleName("카드");
    expect(screen.getByText("카드")).toHaveClass("max-md:sr-only");
    expect(plain).toHaveAccessibleName("글자만");
    expect(screen.getByText("글자만")).not.toHaveClass("max-md:sr-only");

    rerender(<SegmentedControl label="보기" options={options} value="card" onValueChange={() => {}} />);
    expect(screen.getAllByRole("radio")[0]).toHaveAccessibleName("카드");
    expect(screen.getByText("카드")).not.toHaveClass("max-md:sr-only");
  });
});
