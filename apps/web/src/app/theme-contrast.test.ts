/**
 * 테마 대비 검증 (plan §6.4.3, AC-1.2).
 *
 * `globals.css`를 파싱해 3개 프리셋(인디고·틸·뉴트럴) × 라이트·다크 = 6조합의 **유효 토큰**을
 * CSS 캐스케이드(`:root` → `.dark` → `[data-theme]` → `.dark[data-theme]`) 그대로 만들고,
 * 사용처 쌍 U1~U13의 WCAG 2.x 대비를 단언한다. 알파(`/ NN%`, 틴트 `bg-x/NN`)는 아래 면 위에
 * sRGB로 합성한다(브라우저 합성과 같다). 새 의존성 없이 순수 함수로만 계산한다 — 변환식은
 * OKLCH → OKLab → 선형 sRGB(Björn Ottosson 행렬), 상대 휘도·대비는 WCAG 정의.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// ---------- 색 계산 (순수 함수) ----------

type Linear = [number, number, number];
type TokenColor = { lin: Linear; alpha: number };

function oklchToLinear(L: number, C: number, H: number): Linear {
  const h = (H * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.291485548 * b;
  const l = l_ ** 3;
  const m = m_ ** 3;
  const s = s_ ** 3;
  const rgb: Linear = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  // sRGB 색역 밖 값은 브라우저처럼 잘라낸다
  return rgb.map((v) => Math.min(1, Math.max(0, v))) as Linear;
}

const encode = (lin: Linear): Linear =>
  lin.map((v) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)) as Linear;
const decode = (srgb: Linear): Linear =>
  srgb.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)) as Linear;
const luminance = (lin: Linear) => 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];

/** 반투명 전경을 불투명 배경 위에 합성(감마 인코딩된 sRGB 공간 — 브라우저 합성 방식). */
function composite(fg: Linear, bg: Linear, alpha: number): Linear {
  if (alpha >= 1) return fg;
  const f = encode(fg);
  const b = encode(bg);
  return decode(f.map((v, i) => v * alpha + b[i] * (1 - alpha)) as Linear);
}

function contrastRatio(a: Linear, b: Linear): number {
  const x = luminance(a);
  const y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

const OKLCH_RE =
  /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:\/\s*([\d.]+)(%?)\s*)?\)$/;

function parseOklch(value: string): TokenColor {
  const m = value.trim().match(OKLCH_RE);
  if (!m) throw new Error(`지원하지 않는 색 값: ${value}`);
  const alpha = m[4] === undefined ? 1 : m[5] ? Number(m[4]) / 100 : Number(m[4]);
  return { lin: oklchToLinear(Number(m[1]), Number(m[2]), Number(m[3])), alpha };
}

// ---------- globals.css 파싱 · 캐스케이드 ----------

type Blocks = Map<string, Record<string, string>>;

/** 중첩 없는 규칙 블록(`선택자 { --토큰: 값; }`)을 선택자별로 모은다. 같은 선택자는 소스 순서로 덮는다. */
function parseBlocks(css: string): { blocks: Blocks; order: string[] } {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const blocks: Blocks = new Map();
  const order: string[] = [];
  for (const m of stripped.matchAll(/([^{};]+)\{([^{}]*)\}/g)) {
    const selector = m[1].trim();
    const decls = blocks.get(selector) ?? {};
    for (const d of m[2].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) decls[d[1]] = d[2].trim();
    if (!blocks.has(selector)) order.push(selector);
    blocks.set(selector, decls);
  }
  return { blocks, order };
}

const PRESETS = ["indigo", "teal", "neutral"] as const;
type Preset = (typeof PRESETS)[number];
type Mode = "light" | "dark";
type Combo = { preset: Preset; mode: Mode };
const COMBOS: Combo[] = PRESETS.flatMap((preset) =>
  (["light", "dark"] as const).map((mode) => ({ preset, mode })),
);
const comboName = (c: Combo) => `${c.preset}-${c.mode}`;

/** 조합에 적용되는 블록 선택자 — 명시도·소스 순서로 정해지는 실제 캐스케이드 순서. */
function cascadeSelectors({ preset, mode }: Combo): string[] {
  const order = [":root"];
  if (mode === "dark") order.push(".dark");
  if (preset !== "indigo") order.push(`[data-theme="${preset}"]`);
  if (mode === "dark" && preset !== "indigo") order.push(`.dark[data-theme="${preset}"]`);
  return order;
}

function effectiveTokens(blocks: Blocks, combo: Combo): Record<string, string> {
  const tokens: Record<string, string> = {};
  for (const selector of cascadeSelectors(combo)) Object.assign(tokens, blocks.get(selector) ?? {});
  return tokens;
}

// ---------- 사용처 쌍 U1~U13 (§6.4.3) ----------

/** 면: 토큰 그대로(불투명) 또는 `tint` 토큰을 `alpha`만큼 `over`(면) 위에 깐 틴트 — 겹친 틴트도 표현한다. */
type Surface = string | { tint: string; alpha: number; over: Surface };
type Check = { id: string; desc: string; fg: string; fgAlpha?: number; bg: Surface; min: number };

/** danger 톤은 destructive 토큰의 별칭(`@theme inline`의 `--color-danger*` — 아래 별칭 테스트가 고정). */
const TONES = {
  success: { base: "success", text: "success-text", fg: "success-foreground" },
  warning: { base: "warning", text: "warning-text", fg: "warning-foreground" },
  info: { base: "info", text: "info-text", fg: "info-foreground" },
  danger: { base: "destructive", text: "destructive-text", fg: "destructive-foreground" },
} as const;

const tints = (tone: string, alphas: number[], overs: string[]): Surface[] =>
  alphas.flatMap((alpha) => overs.map((over) => ({ tint: tone, alpha, over })));

// ---------- react-flow 캔버스 컨트롤 매핑 (P6 C-4) ----------

/** 라이브러리(`@xyflow/react/dist/style.css`)가 컨트롤 버튼에서 읽는 변수 — `globals.css`의 `.react-flow` 블록이 토큰에 묶는다. */
const FLOW_CONTROL_VARS = {
  bg: "xy-controls-button-background-color",
  bgHover: "xy-controls-button-background-color-hover",
  fg: "xy-controls-button-color",
  fgHover: "xy-controls-button-color-hover",
  border: "xy-controls-button-border-color",
  ring: "xy-controls-box-shadow",
} as const;
type FlowControlTokens = Record<keyof typeof FLOW_CONTROL_VARS, string>;

/**
 * `.react-flow` 블록의 `--xy-controls-*`가 가리키는 테마 토큰 이름. 블록이 없거나(라이브러리 기본 흰 버튼) 값이
 * 토큰 참조(`var(--x)`, 그림자는 끝이 `var(--x)`)가 아니면(하드코딩 색) null — 대비를 6조합에서 계산할 수 없다.
 */
function flowControlTokens(blocks: Blocks): FlowControlTokens | null {
  const block = blocks.get(".react-flow");
  if (!block) return null;
  const out = {} as FlowControlTokens;
  for (const key of Object.keys(FLOW_CONTROL_VARS) as Array<keyof typeof FLOW_CONTROL_VARS>) {
    const m = block[FLOW_CONTROL_VARS[key]]?.match(/var\(--([\w-]+)\)$/);
    if (!m) return null;
    out[key] = m[1];
  }
  return out;
}

function checksFor(mode: Mode, flow: FlowControlTokens | null = null): Check[] {
  const dark = mode === "dark";
  const checks: Check[] = [];
  const add = (id: string, desc: string, fg: string, bgs: Surface[], min: number, fgAlpha?: number) => {
    for (const bg of bgs) checks.push({ id, desc, fg, bg, min, fgAlpha });
  };
  add("U1", "본문", "foreground", ["background", "card"], 4.5);
  add("U2", "보조 글자", "muted-foreground",
    ["background", "card", "sidebar", "sidebar-accent", "muted", "accent"], 4.5);
  add("U3", "primary 버튼 기본·hover", "primary-foreground", ["primary", "primary-hover"], 4.5);
  add("U4", "primary 글자·링크", "primary", ["background", "card"], 4.5);
  add("U5", "사이드바 비활성·hover", "sidebar-foreground", ["sidebar", "sidebar-accent"], 4.5);
  add("U6", "활성 내비", "sidebar-primary", ["sidebar", "sidebar-accent"], 4.5);
  add("U7", "채움 위험 버튼 기본·hover", "destructive-foreground",
    ["destructive", "destructive-hover"], 4.5);
  add("U8a", "위험 글자", "destructive", ["background", "card", "popover"], 4.5);
  // 틴트 위험 버튼(button.tsx destructive): 라이트 10%(기본)·20%(hover), 다크 20%(기본)·30%(hover)
  add("U8b", "틴트 위험 버튼 기본·hover", "destructive-text",
    tints("destructive", dark ? [0.2, 0.3] : [0.1, 0.2], ["background", "card"]), 4.5);
  add("U8c", "틴트 위험 버튼 포커스 테두리", "destructive", ["background", "card"], 3);
  for (const [name, t] of Object.entries(TONES)) {
    // 상태 배지 틴트: 라이트 10%, 다크 15%(10%도 함께 — 더 옅은 틴트를 쓰는 배지 대비)
    add("U9", `${name} 틴트 배지`, t.text,
      tints(t.base, dark ? [0.1, 0.15] : [0.1], ["background", "card"]), 4.5);
    add("U10", `${name} 채움 배지`, t.fg, [t.base], 4.5);
  }
  add("U11", "폼 컨트롤 경계", "input", ["background", "card"], 3);
  add("U12", "포커스 테두리", "ring", ["background", "card"], 3);
  add("U13", "즐겨찾기 별(비텍스트)", "warning", ["card", "background"], 3);
  // ---- 계획 U표 밖 보강(X) — P1이 바꾼 실제 사용처를 같은 기준으로 고정한다 ----
  // X1: 다크에서 밝아진 sidebar-primary 위 글자(현재 미사용 토큰)도 기준을 넘게 둔다
  add("X1", "sidebar-primary 채움 위 글자(미사용 토큰 정합)", "sidebar-primary-foreground",
    ["sidebar-primary"], 4.5);
  const plain = ["background", "card", "popover", "muted", "accent"];
  for (const [name, t] of Object.entries(TONES)) {
    // X2: 하드코딩 팔레트 글자(text-emerald-600 등)를 바꾼 text-{tone}-text — 틴트 없는 면 위
    add("X2", `${name} 글자(틴트 없음)`, t.text, plain, 4.5);
    // X3: 상태 아이콘·즐겨찾기 별(text-{tone}/fill-{tone}) — 비텍스트 3:1, 자기 10% 틴트 위 포함
    add("X3", `${name} 아이콘(비텍스트)`, t.base,
      [...plain, ...tints(t.base, [0.1], ["background", "card"])], 3);
    // X4: hover 틴트(hover:bg-{tone}/20) 위 글자
    add("X4", `${name} hover 틴트 글자`, t.text, tints(t.base, [0.2], ["background", "card"]), 4.5);
  }
  // X5: 경고 배너(bg-warning/10) 안 버튼의 hover(bg-warning/20) — 틴트가 두 겹 쌓인 면
  add("X5", "경고 배너 안 버튼 hover", "warning-text",
    ["background", "card"].map((over) => ({ tint: "warning", alpha: 0.2, over: { tint: "warning", alpha: 0.1, over } })),
    4.5);
  // X6(P2 리뷰 P2-02): 셸 사이드바·드로어의 키보드 포커스 링(`focus-visible:ring-sidebar-ring` — nav-link·사용자 메뉴
  // 트리거·브랜드 링크·검색 트리거·노트북 행·대화 행·「다시 시도」). 링은 요소 밖(사이드바 면)과 요소 면(hover·활성
  // 행의 sidebar-accent, 검색 트리거의 bg-background/60) 사이에 그려지므로 세 면 모두 비텍스트 3:1.
  add("X6", "사이드바 포커스 링(비텍스트)", "sidebar-ring",
    ["sidebar", "sidebar-accent", { tint: "background", alpha: 0.6, over: "sidebar" }], 3);
  // X7(P6 C-4): react-flow 캔버스 컨트롤 — 아이콘(`fill: currentColor`)은 버튼 면 위 비텍스트 3:1(기본·hover),
  // 버튼 묶음 바깥 경계(그림자 링)는 캔버스(--background) 위 3:1. 토큰은 globals.css `.react-flow` 매핑에서 읽는다.
  if (flow) {
    add("X7", "캔버스 컨트롤 아이콘(비텍스트)", flow.fg, [flow.bg], 3);
    add("X7", "캔버스 컨트롤 hover 아이콘(비텍스트)", flow.fgHover, [flow.bgHover], 3);
    add("X7", "캔버스 컨트롤 바깥 경계(비텍스트)", flow.ring, ["background"], 3);
  }
  // M1(mindmap-agent-skill AC-1.3): 마인드맵 깊이별 노드 면(--mindmap-depth-0..2) 위 노드 라벨(--foreground)
  add("M1", "마인드맵 깊이별 노드 라벨", "foreground",
    ["mindmap-depth-0", "mindmap-depth-1", "mindmap-depth-2"], 4.5);
  return checks;
}

type Result = Check & { combo: string; bgLabel: string; ratio: number };

function evaluate(blocks: Blocks, combo: Combo): Result[] {
  const tokens = effectiveTokens(blocks, combo);
  const color = (name: string): TokenColor => {
    const v = tokens[name];
    if (v === undefined) throw new Error(`${comboName(combo)}: 토큰 --${name} 없음`);
    return parseOklch(v);
  };
  // 토큰 자체의 알파는 그 토큰이 놓이는 면 위에 합성한다(예: 다크 --border 12%)
  const solid = (name: string, under: Linear): Linear => {
    const c = color(name);
    return composite(c.lin, under, c.alpha);
  };
  const page = solid("background", oklchToLinear(1, 0, 0));
  const surface = (s: Surface): Linear => {
    if (typeof s === "string") return solid(s, page);
    const t = color(s.tint);
    return composite(t.lin, surface(s.over), t.alpha * s.alpha);
  };
  const label = (s: Surface): string =>
    typeof s === "string" ? s : `${s.tint} ${Math.round(s.alpha * 100)}% / ${label(s.over)}`;
  return checksFor(combo.mode, flowControlTokens(blocks)).map((check) => {
    const bg = surface(check.bg);
    const fgColor = color(check.fg);
    const fg = composite(fgColor.lin, bg, fgColor.alpha * (check.fgAlpha ?? 1));
    return { ...check, combo: comboName(combo), bgLabel: label(check.bg), ratio: contrastRatio(fg, bg) };
  });
}

const failures = (results: Result[]) =>
  results
    .filter((r) => r.ratio < r.min)
    .map((r) => `${r.combo} ${r.id}(${r.desc}) ${r.fg} / ${r.bgLabel} = ${r.ratio.toFixed(2)} < ${r.min}`);

function evaluateAll(blocks: Blocks): Result[] {
  return COMBOS.flatMap((combo) => evaluate(blocks, combo));
}

// ---------- 테스트 ----------

const CSS = readFileSync(path.resolve(__dirname, "globals.css"), "utf8");
const { blocks: BLOCKS, order: ORDER } = parseBlocks(CSS);
const RESULTS = evaluateAll(BLOCKS);

if (process.env.THEME_CONTRAST_REPORT) {
  // 보고서용 최솟값 표(평소에는 출력하지 않는다)
  const byId = new Map<string, Result>();
  for (const r of RESULTS) {
    const cur = byId.get(r.id);
    if (!cur || r.ratio < cur.ratio) byId.set(r.id, r);
  }
  for (const r of byId.values()) {
    console.info(`[contrast-min] ${r.id} ${r.ratio.toFixed(2)} (기준 ${r.min}) ${r.combo} ${r.fg} / ${r.bgLabel}`);
  }
}

describe("globals.css 캐스케이드 전제", () => {
  it("6조합에 필요한 블록이 모두 있고, .dark가 프리셋 블록보다 앞에 있다(명시도 동률 → 소스 순서)", () => {
    const required = [
      ":root",
      ".dark",
      '[data-theme="teal"]',
      '.dark[data-theme="teal"]',
      '[data-theme="neutral"]',
      '.dark[data-theme="neutral"]',
    ];
    for (const s of required) expect(ORDER, `블록 누락: ${s}`).toContain(s);
    const darkAt = ORDER.indexOf(".dark");
    for (const p of ['[data-theme="teal"]', '[data-theme="neutral"]']) {
      expect(darkAt, `${p}는 .dark 뒤에 있어야 캐스케이드 해석이 맞다`).toBeLessThan(ORDER.indexOf(p));
      expect(ORDER.indexOf(p)).toBeLessThan(ORDER.indexOf(`.dark${p}`));
    }
  });

  it("프리셋 라이트 블록의 토큰은 프리셋 다크 블록에도 있다(차트 색 제외) — 다크에 라이트 값이 새지 않음", () => {
    for (const p of ["teal", "neutral"]) {
      const light = Object.keys(BLOCKS.get(`[data-theme="${p}"]`) ?? {}).filter((k) => !k.startsWith("chart-"));
      const dark = BLOCKS.get(`.dark[data-theme="${p}"]`) ?? {};
      const leaked = light.filter((k) => !(k in dark));
      expect(leaked, `${p} 다크에 재정의되지 않은 라이트 토큰`).toEqual([]);
    }
  });

  it("danger 톤 유틸은 destructive 토큰의 별칭이다", () => {
    const theme = CSS.match(/@theme inline\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(theme).toMatch(/--color-danger:\s*var\(--destructive\);/);
    expect(theme).toMatch(/--color-danger-foreground:\s*var\(--destructive-foreground\);/);
    expect(theme).toMatch(/--color-danger-text:\s*var\(--destructive-text\);/);
  });
});

describe.each(COMBOS.map((c) => [comboName(c), c] as const))("대비 %s", (name) => {
  const ids = [...new Set(RESULTS.filter((r) => r.combo === name).map((r) => r.id))];
  it.each(ids)("%s 기준 충족", (id) => {
    const rows = RESULTS.filter((r) => r.combo === name && r.id === id);
    expect(rows.length).toBeGreaterThan(0);
    expect(failures(rows)).toEqual([]);
  });
});

describe("대비 기록·회귀 픽스처", () => {
  it("U8b 최솟값은 4.74(뉴트럴 다크 · 카드 · hover 30%)로 기록한다 — 기준은 4.5", () => {
    const u8b = RESULTS.filter((r) => r.id === "U8b");
    const min = u8b.reduce((a, b) => (b.ratio < a.ratio ? b : a));
    expect(min.ratio).toBeCloseTo(4.74, 2);
    expect(min.combo).toBe("neutral-dark");
    expect(min.bgLabel).toBe("destructive 30% / card");
  });

  it("계산기는 계획 §6.4.3 「대표 계산값」 표의 제안 최솟값을 재현한다(독립 재계산 교차 확인)", () => {
    const minOf = (id: string, bg?: string) =>
      Math.min(...RESULTS.filter((r) => r.id === id && (!bg || r.bgLabel === bg)).map((r) => r.ratio));
    const expected: Array<[string, number, string?]> = [
      ["U2", 5.04], ["U3", 5.57], ["U4", 5.65], ["U6", 4.91], ["U7", 5.89], ["U8a", 5.97],
      ["U8b", 4.74], ["U8c", 5.97], ["U9", 5.8], ["U11", 3.08], ["U12", 3.1], ["U13", 4.98, "card"],
    ];
    for (const [id, value, bg] of expected) expect(minOf(id, bg), id).toBeCloseTo(value, 2);
  });

  /** 블록 값 일부를 바꾼 사본 — 일부러 되돌린 토큰에서 검사가 실패하는지 확인한다. */
  const withTokens = (overrides: Array<[selector: string, token: string, value: string]>): Blocks => {
    const copy: Blocks = new Map([...BLOCKS].map(([k, v]) => [k, { ...v }]));
    for (const [selector, token, value] of overrides) {
      const block = copy.get(selector);
      if (!block || !(token in block)) throw new Error(`픽스처 대상 없음: ${selector} --${token}`);
      block[token] = value;
    }
    return copy;
  };

  it("픽스처: 틸 라이트 --primary를 현행 0.55로 되돌리면 실패한다(U3·U4)", () => {
    const fx = withTokens([['[data-theme="teal"]', "primary", "oklch(0.55 0.12 192)"]]);
    const failed = failures(evaluateAll(fx));
    expect(failed.some((f) => f.startsWith("teal-light U3("))).toBe(true);
    expect(failed.some((f) => f.startsWith("teal-light U4("))).toBe(true);
  });

  it("픽스처: 사이드바 포커스 링을 P2 리뷰 전 값으로 되돌리면 틸·뉴트럴 라이트에서 실패한다(X6, P2-02)", () => {
    const fx = withTokens([
      ['[data-theme="teal"]', "sidebar-ring", "oklch(0.64 0.11 192)"],
      ['[data-theme="neutral"]', "sidebar-ring", "oklch(0.708 0 0)"],
    ]);
    const failed = failures(evaluateAll(fx)).filter((f) => f.includes(" X6("));
    expect(failed).toEqual([
      "teal-light X6(사이드바 포커스 링(비텍스트)) sidebar-ring / sidebar = 2.97 < 3",
      "teal-light X6(사이드바 포커스 링(비텍스트)) sidebar-ring / sidebar-accent = 2.70 < 3",
      "neutral-light X6(사이드바 포커스 링(비텍스트)) sidebar-ring / sidebar = 2.48 < 3",
      "neutral-light X6(사이드바 포커스 링(비텍스트)) sidebar-ring / sidebar-accent = 2.38 < 3",
      "neutral-light X6(사이드바 포커스 링(비텍스트)) sidebar-ring / background 60% / sidebar = 2.55 < 3",
    ]);
  });

  it("픽스처: 틴트 위험 버튼 글자를 destructive로 되돌리면 hover에서 실패한다(U8b)", () => {
    const fx = withTokens([
      [":root", "destructive-text", BLOCKS.get(":root")!["destructive"]],
      [".dark", "destructive-text", BLOCKS.get(".dark")!["destructive"]],
    ]);
    const failed = failures(evaluateAll(fx)).filter((f) => f.includes(" U8b("));
    expect(failed.some((f) => f.includes("-light ") && f.includes("destructive 20%"))).toBe(true);
    expect(failed.some((f) => f.includes("-dark ") && f.includes("destructive 30%"))).toBe(true);
  });
});

describe("react-flow 캔버스 컨트롤 (P6 C-4 — 다크에서 흰 버튼·안 보이는 아이콘)", () => {
  it("globals.css `.react-flow`가 라이브러리 컨트롤 변수 6개를 모두 테마 토큰에 묶는다(하드코딩 색 0)", () => {
    expect(flowControlTokens(BLOCKS)).toEqual({
      bg: "card",
      bgHover: "accent",
      fg: "card-foreground",
      fgHover: "accent-foreground",
      border: "border",
      ring: "input",
    });
  });

  it("X7 대비를 6조합 모두에서 계산했다(아이콘·hover 아이콘·바깥 경계 각 1행 × 6)", () => {
    const x7 = RESULTS.filter((r) => r.id === "X7");
    expect(x7).toHaveLength(3 * COMBOS.length);
    expect(new Set(x7.map((r) => r.combo))).toEqual(new Set(COMBOS.map(comboName)));
    expect(failures(x7)).toEqual([]);
  });

  it("픽스처: 매핑이 없거나 하드코딩 색이면 토큰을 얻지 못한다(null)", () => {
    const without: Blocks = new Map([...BLOCKS].filter(([k]) => k !== ".react-flow"));
    expect(flowControlTokens(without)).toBeNull();
    const hard: Blocks = new Map([...BLOCKS].map(([k, v]) => [k, { ...v }]));
    hard.get(".react-flow")![FLOW_CONTROL_VARS.bg] = "#fefefe";
    expect(flowControlTokens(hard)).toBeNull();
  });

  it("픽스처: 라이브러리 기본값(흰 바탕 #fefefe + 상속 본문 글자)은 다크 3조합에서 아이콘 3:1 미만이다 — 고치기 전 결함 재현", () => {
    const libraryWhite = parseOklch("oklch(0.997 0 0)").lin; // #fefefe
    for (const combo of COMBOS) {
      const fg = parseOklch(effectiveTokens(BLOCKS, combo).foreground).lin;
      const ratio = contrastRatio(fg, libraryWhite);
      if (combo.mode === "dark") expect(ratio, comboName(combo)).toBeLessThan(3);
      else expect(ratio, comboName(combo)).toBeGreaterThanOrEqual(3);
    }
  });
});

describe("색 계산 순수 함수", () => {
  it("흰색·검정 대비는 21:1, 같은 색은 1:1", () => {
    const white = oklchToLinear(1, 0, 0);
    const black = oklchToLinear(0, 0, 0);
    expect(contrastRatio(white, black)).toBeCloseTo(21, 1);
    expect(contrastRatio(white, white)).toBeCloseTo(1, 5);
  });

  it("알파 표기(퍼센트·소수)를 해석하고, 알파 1은 합성하지 않는다", () => {
    expect(parseOklch("oklch(1 0 0 / 16%)").alpha).toBeCloseTo(0.16, 5);
    expect(parseOklch("oklch(1 0 0 / 0.5)").alpha).toBeCloseTo(0.5, 5);
    const c = oklchToLinear(0.5, 0.1, 200);
    expect(composite(c, oklchToLinear(1, 0, 0), 1)).toEqual(c);
  });

  it("지원하지 않는 값은 조용히 넘기지 않고 오류를 낸다", () => {
    expect(() => parseOklch("var(--primary)")).toThrow(/지원하지 않는 색 값/);
  });
});
