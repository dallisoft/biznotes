import { Checkbox as CheckboxPrimitive } from "@base-ui/react/checkbox";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CheckIcon } from "lucide-react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { cn } from "@/lib/utils";
import { Checkbox } from "./checkbox";

// AC-1.9 — 기존 사용처의 렌더는 바뀌지 않는다. 혼합 표시(가로줄)는 `mixedIndicator`를 준 곳에서만 쓴다(P1-01).

/** 변경 전(HEAD 925cbef) `ui/checkbox.tsx` 원본 그대로 — 호환성 대조 기준. */
function LegacyCheckbox({ className, ...props }: CheckboxPrimitive.Root.Props) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer relative flex size-4 shrink-0 items-center justify-center rounded-[4px] border border-input transition-colors outline-none group-has-disabled/field:opacity-50 after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 aria-invalid:aria-checked:border-primary dark:bg-input/30 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground dark:data-checked:bg-primary",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current transition-none [&>svg]:size-3.5"
      >
        <CheckIcon />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

/** React·Base UI가 렌더마다 새로 만드는 id 값만 지우고 나머지 마크업을 그대로 비교한다. */
function markup(element: React.ReactElement) {
  const { container, unmount } = render(element);
  const html = container.innerHTML.replace(
    /\s(id|for|aria-labelledby|aria-describedby)="[^"]*"/g,
    ' $1="_"',
  );
  unmount();
  return html;
}

function indicatorIcon(checkbox: HTMLElement) {
  return checkbox.querySelector('[data-slot="checkbox-indicator"] svg');
}

const LEGACY_CASES: { name: string; props: CheckboxPrimitive.Root.Props }[] = [
  { name: "unchecked", props: { checked: false } },
  { name: "checked", props: { checked: true } },
  // 소스 추가 대화상자 「전체 선택」(add-source-dialog.tsx)의 일부 선택 상태
  { name: "indeterminate", props: { checked: false, indeterminate: true } },
  { name: "checked+indeterminate", props: { checked: true, indeterminate: true } },
  { name: "disabled checked", props: { checked: true, disabled: true } },
  { name: "className", props: { checked: true, className: "mt-0.5" } },
];

describe("Checkbox — 기존 사용처 호환(mixedIndicator 없음)", () => {
  it.each(LEGACY_CASES)("$name: 변경 전 컴포넌트와 마크업이 같다", ({ props }) => {
    expect(markup(<Checkbox aria-label="항목" {...props} />)).toBe(
      markup(<LegacyCheckbox aria-label="항목" {...props} />),
    );
  });

  it("대조군 — 같은 비교가 mixedIndicator의 표시 차이는 잡아낸다(검출기 유효성)", () => {
    const props = { checked: false, indeterminate: true };
    expect(markup(<Checkbox aria-label="항목" mixedIndicator {...props} />)).not.toBe(
      markup(<LegacyCheckbox aria-label="항목" {...props} />),
    );
  });

  it("indeterminate여도 기존처럼 CheckIcon만 그리고 혼합 채움 클래스를 붙이지 않는다", () => {
    render(<Checkbox aria-label="전체 선택" checked={false} indeterminate />);
    const checkbox = screen.getByRole("checkbox", { name: "전체 선택" });
    expect(checkbox).toHaveAttribute("aria-checked", "mixed");
    expect(indicatorIcon(checkbox)).toHaveClass("lucide-check");
    expect(checkbox.querySelector('[data-icon="indeterminate"]')).toBeNull();
    expect(checkbox.className).not.toContain("data-indeterminate:bg-primary");
  });

  it("unchecked면 표시 아이콘이 없다", () => {
    render(<Checkbox aria-label="항목" checked={false} />);
    const checkbox = screen.getByRole("checkbox", { name: "항목" });
    expect(checkbox).toHaveAttribute("aria-checked", "false");
    expect(indicatorIcon(checkbox)).toBeNull();
  });

  it("제어 사용처는 클릭으로 토글되고 CheckIcon을 그린다", async () => {
    const user = userEvent.setup();
    function Controlled() {
      const [value, setValue] = useState(false);
      return (
        <>
          <Checkbox aria-label="토글" checked={value} onCheckedChange={setValue} />
          <output>{String(value)}</output>
        </>
      );
    }
    render(<Controlled />);
    const checkbox = screen.getByRole("checkbox", { name: "토글" });
    await user.click(checkbox);
    expect(screen.getByText("true")).toBeInTheDocument();
    expect(indicatorIcon(checkbox)).toHaveClass("lucide-check");
  });
});

describe("Checkbox — mixedIndicator(노트북 「모두 선택」)", () => {
  it("indeterminate면 aria-checked=mixed, 가로줄 아이콘, 채움 클래스", () => {
    render(<Checkbox aria-label="모두" checked={false} indeterminate mixedIndicator />);
    const checkbox = screen.getByRole("checkbox", { name: "모두" });
    expect(checkbox).toHaveAttribute("aria-checked", "mixed");
    expect(checkbox).toHaveAttribute("data-indeterminate");
    const icon = indicatorIcon(checkbox);
    expect(icon).toHaveAttribute("data-icon", "indeterminate");
    expect(icon).toHaveClass("lucide-minus");
    expect(icon).not.toHaveClass("lucide-check");
    expect(checkbox.className).toContain("data-indeterminate:bg-primary");
    expect(checkbox).not.toHaveAttribute("mixedindicator"); // DOM 속성으로 새지 않는다
  });

  it("indeterminate가 아니면 checked 표시는 기존과 같은 CheckIcon이다", () => {
    render(<Checkbox aria-label="모두" checked mixedIndicator />);
    const checkbox = screen.getByRole("checkbox", { name: "모두" });
    expect(checkbox).toHaveAttribute("aria-checked", "true");
    expect(checkbox).toHaveAttribute("data-checked");
    expect(indicatorIcon(checkbox)).toHaveClass("lucide-check");
    expect(checkbox.querySelector('[data-icon="indeterminate"]')).toBeNull();
  });
});
