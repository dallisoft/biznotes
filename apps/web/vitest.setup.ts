/// <reference types="@testing-library/jest-dom/vitest" />

import * as matchers from "@testing-library/jest-dom/matchers";
import { cleanup } from "@testing-library/react";
import { afterEach, expect } from "vitest";

// pnpm hoisting can expose jest-dom's `vitest` entrypoint to a different
// Vitest module instance. Extend this workspace's test-global `expect`
// explicitly so the DOM matchers are available in every test worker.
expect.extend(matchers);

// Node 22+의 jsdom 29는 `--localstorage-file` 없이 localStorage를 노출하지 않는다.
// 브라우저 저장소를 사용하는 컴포넌트 테스트는 파일 영속성이 필요 없으므로 메모리
// 구현을 제공해 테스트 실행 환경을 브라우저 계약에 맞춘다.
if (typeof globalThis.localStorage === "undefined") {
  const values = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      key: (index: number) => [...values.keys()][index] ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, String(value)),
      get length() {
        return values.size;
      },
    },
  });
}

// jsdom에는 ResizeObserver가 없다 — @xyflow/react(react-flow) 캔버스가 마운트 시
// 크기 관측을 위해 사용하므로, 이를 사용하는 컴포넌트 테스트 전반에서 필요하다.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom에는 Element.getAnimations가 없다 — @base-ui/react의 ScrollArea 뷰포트가
// 스크롤바 자동 숨김 타이머에서 호출하므로, ScrollArea를 렌더하는 테스트 전반에서 필요하다.
if (typeof Element !== "undefined" && !Element.prototype.getAnimations) {
  Element.prototype.getAnimations = () => [];
}

afterEach(() => {
  cleanup();
});
