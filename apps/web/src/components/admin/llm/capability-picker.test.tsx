import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { CapabilityPicker, MODALITIES, toggleCapability } from "./capability-picker";

function Harness({ initial, onChange }: { initial: string[]; onChange: (v: string[]) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <CapabilityPicker
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

// 체크박스 이름은 감싼 <label> 전체(예: 「reranker (단독)」) — 능력 이름 접두로 찾는다
const box = (modality: string) =>
  screen.getByRole("checkbox", { name: new RegExp(`^${modality}\\b`) });

describe("MODALITIES", () => {
  it("코어 정본 순서 — image는 맨 끝(정본 대조는 apps/api/tests/test_web_modalities_contract.py)", () => {
    expect(MODALITIES).toEqual(["llm", "vlm", "embedding", "reranker", "image"]);
  });
});

describe("toggleCapability", () => {
  it("reranker를 고르면 다른 능력을 모두 해제한다", () => {
    expect(toggleCapability(["llm", "vlm"], "reranker")).toEqual(["reranker"]);
  });

  it("다른 능력을 고르면 reranker를 해제한다", () => {
    expect(toggleCapability(["reranker"], "llm")).toEqual(["llm"]);
  });

  it("일반 능력은 기존처럼 중복 선택·해제된다", () => {
    expect(toggleCapability(["llm"], "vlm")).toEqual(["llm", "vlm"]);
    expect(toggleCapability(["llm", "vlm"], "llm")).toEqual(["vlm"]);
    expect(toggleCapability(["reranker"], "reranker")).toEqual([]);
  });

  it("image도 단독 능력이다(코어 v0.23.0 #338) — 고르면 나머지를 해제하고 reranker와도 섞이지 않는다", () => {
    expect(toggleCapability(["llm", "vlm"], "image")).toEqual(["image"]);
    expect(toggleCapability(["image"], "llm")).toEqual(["llm"]);
    expect(toggleCapability(["reranker"], "image")).toEqual(["image"]);
    expect(toggleCapability(["image"], "reranker")).toEqual(["reranker"]);
  });
});

describe("CapabilityPicker", () => {
  it("reranker 체크박스와 「(단독)」 라벨을 보여 준다", () => {
    render(<CapabilityPicker value={["llm"]} onChange={vi.fn()} />);

    expect(box("reranker")).toBeInTheDocument();
    expect(box("reranker")).not.toBeChecked();
    expect(box("image")).toBeInTheDocument();
    expect(screen.getAllByText("(단독)")).toHaveLength(2);
    expect(screen.getByText("(전역 1개)")).toBeInTheDocument();
  });

  it("reranker를 체크하면 llm·vlm이 해제되고, 다시 llm을 체크하면 reranker가 해제된다", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={["llm", "vlm"]} onChange={onChange} />);

    await user.click(box("reranker"));
    expect(onChange).toHaveBeenLastCalledWith(["reranker"]);
    expect(box("reranker")).toBeChecked();
    expect(box("llm")).not.toBeChecked();
    expect(box("vlm")).not.toBeChecked();

    await user.click(box("llm"));
    expect(onChange).toHaveBeenLastCalledWith(["llm"]);
    expect(box("llm")).toBeChecked();
    expect(box("reranker")).not.toBeChecked();
  });
});
