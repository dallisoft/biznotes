import { act, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DASHBOARD_VIEW_STORAGE_KEY,
  parseDashboardViewMode,
  useDashboardViewMode,
  ViewModeToggle,
} from "./view-mode-toggle";

// AC-1.2 — 뷰 모드는 localStorage에 저장·복원되고, 저장값이 없거나 잘못되면 card, hydration 경고가 없다.
// ui-ux-redesign P3(plan §6.6.2 노트북 홈 「보기 전환」, §6.5 SegmentedControl) — 「카드 | 표」 radiogroup.

function Harness() {
  const [mode, setMode] = useDashboardViewMode();
  return <ViewModeToggle value={mode} onChange={setMode} />;
}

function checked(name: string) {
  return screen.getByRole("radio", { name }).getAttribute("aria-checked");
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("parseDashboardViewMode", () => {
  it("table만 table이고 나머지는 card다", () => {
    expect(parseDashboardViewMode("table")).toBe("table");
    expect(parseDashboardViewMode("card")).toBe("card");
    expect(parseDashboardViewMode(null)).toBe("card");
    expect(parseDashboardViewMode("")).toBe("card");
    expect(parseDashboardViewMode("grid")).toBe("card");
    expect(parseDashboardViewMode("TABLE")).toBe("card");
  });
});

describe("ViewModeToggle — 공용 SegmentedControl(「카드 | 표」)", () => {
  it("radiogroup 「보기 방식」 안에 「카드」「표」 두 radio이고, 기존 e2e testid를 유지한다(D-17)", () => {
    render(<ViewModeToggle value="card" onChange={() => {}} />);
    const group = screen.getByRole("radiogroup", { name: "보기 방식" });
    expect(group).toHaveClass("whitespace-nowrap");
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.textContent)).toEqual(["카드", "표"]);
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false"]);
    expect(screen.getByTestId("view-mode-card")).toBe(radios[0]);
    expect(screen.getByTestId("view-mode-table")).toBe(radios[1]);
    // 예전 토글 버튼(aria-pressed)은 없다
    expect(screen.queryByRole("button", { name: "테이블" })).toBeNull();
  });

  it("좁은 화면에서는 아이콘만 보이고 라벨은 화면 낭독기용으로 남는다(목업 M1, P3 IR-H5)", () => {
    render(<ViewModeToggle value="card" onChange={() => {}} />);
    for (const name of ["카드", "표"]) {
      expect(screen.getByRole("radio", { name })).toBeInTheDocument();
      expect(screen.getByText(name)).toHaveClass("max-md:sr-only");
    }
  });

  it("화살표 키로 옮기면 보기가 바뀌고 저장된다", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("radio", { name: "카드" }));
    await user.keyboard("{ArrowRight}");
    expect(checked("표")).toBe("true");
    expect(localStorage.getItem(DASHBOARD_VIEW_STORAGE_KEY)).toBe("table");
  });
});

describe("useDashboardViewMode", () => {
  it("저장값이 없으면 card", () => {
    const { result } = renderHook(() => useDashboardViewMode());
    expect(result.current[0]).toBe("card");
  });

  it("저장된 table을 마운트 뒤 복원한다", () => {
    localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, "table");
    const { result } = renderHook(() => useDashboardViewMode());
    expect(result.current[0]).toBe("table");
  });

  it("잘못된 저장값이면 card", () => {
    localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, "{bogus");
    const { result } = renderHook(() => useDashboardViewMode());
    expect(result.current[0]).toBe("card");
  });

  it("전환하면 저장하고, 새로 마운트(새로고침)해도 복원된다 — 저장값 형식은 card·table 그대로(plan §8.2)", async () => {
    const user = userEvent.setup();
    const first = render(<Harness />);
    expect(checked("카드")).toBe("true");
    await user.click(screen.getByRole("radio", { name: "표" }));
    expect(checked("표")).toBe("true");
    expect(localStorage.getItem(DASHBOARD_VIEW_STORAGE_KEY)).toBe("table");
    first.unmount();

    render(<Harness />);
    expect(checked("표")).toBe("true");
    expect(checked("카드")).toBe("false");
  });

  it("저장소 접근이 실패해도 card로 시작하고 화면 전환은 동작한다", async () => {
    vi.spyOn(localStorage, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(localStorage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const user = userEvent.setup();
    render(<Harness />);
    expect(checked("카드")).toBe("true");
    await user.click(screen.getByRole("radio", { name: "표" }));
    expect(checked("표")).toBe("true");
  });
});

describe("hydration", () => {
  it("저장값이 table이어도 서버 HTML과 첫 클라이언트 렌더가 같아 hydration 오류가 없고, 마운트 뒤 table이 된다", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    container.innerHTML = renderToString(<Harness />); // 서버 렌더 — 저장소 없음
    // 서버 HTML은 card 선택 상태다
    expect(container.querySelector('[data-testid="view-mode-card"]')?.getAttribute("aria-checked")).toBe("true");
    localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, "table");
    const recoverable = vi.fn();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    let root: ReturnType<typeof hydrateRoot> | undefined;
    await act(async () => {
      root = hydrateRoot(container, <Harness />, { onRecoverableError: recoverable });
    });
    const errors = [...recoverable.mock.calls, ...consoleError.mock.calls];
    const table = container.querySelector('[data-testid="view-mode-table"]');
    const tableChecked = table?.getAttribute("aria-checked");
    act(() => root?.unmount());
    container.remove();
    expect(errors).toEqual([]);
    expect(tableChecked).toBe("true");
  });

  it("대조군 — 렌더 중에 저장소를 읽는 구현이면 같은 하네스가 hydration 불일치를 잡아낸다", async () => {
    let onClient = false; // 서버 렌더 때는 false, hydrate 때는 true
    function NaiveToggle() {
      const mode = onClient
        ? parseDashboardViewMode(localStorage.getItem(DASHBOARD_VIEW_STORAGE_KEY))
        : "card";
      return <ViewModeToggle value={mode} onChange={() => {}} />;
    }
    const container = document.createElement("div");
    document.body.appendChild(container);
    container.innerHTML = renderToString(<NaiveToggle />);
    onClient = true;
    localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, "table");
    const recoverable = vi.fn();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    let root: ReturnType<typeof hydrateRoot> | undefined;
    await act(async () => {
      root = hydrateRoot(container, <NaiveToggle />, { onRecoverableError: recoverable });
    });
    // React 19는 속성 불일치를 console.error 경고로, 텍스트·구조 불일치를 onRecoverableError로 알린다
    const reported = [...recoverable.mock.calls, ...consoleError.mock.calls].map((args) =>
      args.map(String).join(" "),
    );
    expect(reported.some((msg) => /hydrat/i.test(msg))).toBe(true);
    act(() => root?.unmount());
    container.remove();
  });
});
