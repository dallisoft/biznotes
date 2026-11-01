/**
 * 마인드맵 산출물 관용 파서(mindmap-agent-skill AC-1.1, D-10).
 *
 * - 순수 JSON, ```json 펜스, 앞뒤에 설명 문장이 붙은 JSON(첫 `{`와 짝 `}`)을 해석한다.
 * - 형태 판정은 코어 `_validate_mindmap_shape`와 같다 — 공유 fixture(`lib/__fixtures__/mindmap/`)를
 *   웹(parse.test.ts)과 코어(apps/api/tests/test_mindmap_contract.py)가 함께 읽어 판정을 대조한다.
 * - 노드가 200개를 넘으면 코어 `_truncate_mindmap_bfs`와 같은 BFS 절단을 하고 `truncated`를 켠다.
 * - 모든 순회는 반복문이다(재귀 없음) — 병적으로 깊은 트리는 소비 경계(bounded.ts)가 먼저 막는다.
 */
import { bounded } from "@/lib/mindmap/bounded";

export const MINDMAP_NODE_CAP = 200;
export const MINDMAP_LABEL_DISPLAY_CHARS = 120;

export interface MindmapSourceRef {
  source_id: string;
  chunk_id?: string;
}

export interface MindmapNode {
  id: string;
  /** 표시용 라벨 — 정화 + 120자 */
  label: string;
  /** 정화한 전체 라벨(툴팁·내보내기) */
  title: string;
  source_ref?: MindmapSourceRef;
  children: MindmapNode[];
}

export interface MindmapDoc {
  version?: number;
  root: MindmapNode;
  truncated: boolean;
}

export type MindmapParseFailure =
  | { ok: false; reason: "too_large" | "too_deep"; preview: string }
  | { ok: false; reason: "invalid_json" | "invalid_shape"; detail: string };

export type MindmapParseResult = { ok: true; doc: MindmapDoc } | MindmapParseFailure;

type Json = unknown;
type RawNode = Record<string, Json>;

// Python str.isspace()와 같은 공백 집합 — 코어 `label.strip()` 판정과 맞춘다.
const PY_SPACE =
  "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";

function pyStripIsEmpty(s: string): boolean {
  for (const ch of s) {
    if (!PY_SPACE.includes(ch)) return false;
  }
  return true;
}

function isObject(v: Json): v is RawNode {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 코어 `_validate_mindmap_shape`와 같은 판정. 실패 사유 또는 null(통과). */
export function validateMindmapShape(data: Json): string | null {
  if (!isObject(data)) return "최상위 값이 객체가 아닙니다";
  const root = data.root;
  if (!isObject(root)) return "root 필드가 객체가 아닙니다";
  const seen = new Set<string>();
  const stack: [Json, string][] = [[root, "root"]];
  while (stack.length > 0) {
    const [n, path] = stack.pop() as [Json, string];
    if (!isObject(n)) return `${path}가 객체가 아닙니다`;
    const id = n.id;
    if (typeof id !== "string" || pyStripIsEmpty(id)) return `${path}.id가 비어있는 문자열입니다`;
    if (seen.has(id)) return `${path}.id가 트리 내에서 중복됩니다: ${id}`;
    seen.add(id);
    const label = n.label;
    if (typeof label !== "string" || pyStripIsEmpty(label)) {
      return `${path}.label이 비어있는 문자열입니다`;
    }
    const children = "children" in n ? n.children : [];
    if (!Array.isArray(children)) return `${path}.children이 배열이 아닙니다`;
    children.forEach((child, i) => stack.push([child, `${path}.children[${i}]`]));
  }
  return null;
}

export function countMindmapNodes(root: RawNode): number {
  let count = 0;
  const stack: RawNode[] = [root];
  while (stack.length > 0) {
    const n = stack.pop() as RawNode;
    count += 1;
    const children = Array.isArray(n.children) ? n.children : [];
    for (const c of children) if (isObject(c)) stack.push(c);
  }
  return count;
}

/** 코어 `_truncate_mindmap_bfs`와 같은 BFS 절단(제자리 변경). [root, truncated]. */
export function truncateMindmapBfs(root: RawNode, cap: number): [RawNode, boolean] {
  let kept = 1;
  let truncated = false;
  const queue: RawNode[] = [root];
  let head = 0;
  while (head < queue.length) {
    const node = queue[head];
    head += 1;
    const raw = Array.isArray(node.children) ? node.children : [];
    const next: RawNode[] = [];
    for (const child of raw) {
      if (!isObject(child)) continue;
      if (kept >= cap) {
        truncated = true;
        continue;
      }
      kept += 1;
      next.push(child);
      queue.push(child);
    }
    node.children = next;
  }
  return [root, truncated];
}

// 제어(Cc)·형식(Cf) 문자 — 공백류는 먼저 한 칸으로 접는다.
const WHITESPACE_RUN = /[\s\x1c-\x1f\x85]+/gu;
const CONTROL_FORMAT = /[\p{Cc}\p{Cf}]/gu;

/** 라벨 정화: 공백·줄바꿈 접기, Cc·Cf 제거, 앞뒤 공백 제거. */
export function sanitizeLabel(raw: string): string {
  return raw.replace(WHITESPACE_RUN, " ").replace(CONTROL_FORMAT, "").trim();
}

export function truncateChars(text: string, max: number): string {
  const chars = Array.from(text);
  if (chars.length <= max) return text;
  return `${chars.slice(0, max - 1).join("")}…`;
}

/** 첫 `{`부터 문자열을 고려해 짝이 맞는 `}`까지 잘라낸다. 없으면 null. */
function sliceFirstObject(text: string): string | null {
  const start = text.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}

const FENCE = /```[a-zA-Z]*[ \t]*\r?\n?([\s\S]*?)```/;

function tryParse(text: string): { ok: true; value: Json } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}

/** 관용 JSON 해석 — 원문 → 펜스 안 → 첫 객체 순. 경계(bounded)를 통과한 텍스트에만 쓴다. */
export function parseLenientJson(text: string): { ok: true; value: Json } | { ok: false } {
  const direct = tryParse(text.trim());
  if (direct.ok) return direct;
  const fence = FENCE.exec(text);
  if (fence) {
    const inner = tryParse(fence[1].trim());
    if (inner.ok) return inner;
  }
  const sliced = sliceFirstObject(text);
  if (sliced) return tryParse(sliced);
  return { ok: false };
}

function normalizeNode(raw: RawNode): MindmapNode {
  const title = sanitizeLabel(String(raw.label));
  const node: MindmapNode = {
    id: String(raw.id),
    label: truncateChars(title, MINDMAP_LABEL_DISPLAY_CHARS),
    title,
    children: [],
  };
  const ref = raw.source_ref;
  if (isObject(ref) && typeof ref.source_id === "string" && ref.source_id.length > 0) {
    node.source_ref = { source_id: ref.source_id };
    if (typeof ref.chunk_id === "string") node.source_ref.chunk_id = ref.chunk_id;
  }
  return node;
}

/** 검증을 통과한 원시 트리를 표시용 트리로 바꾼다(반복 순회). */
function normalizeTree(root: RawNode): MindmapNode {
  const out = normalizeNode(root);
  const stack: [RawNode, MindmapNode][] = [[root, out]];
  while (stack.length > 0) {
    const [raw, node] = stack.pop() as [RawNode, MindmapNode];
    const children = Array.isArray(raw.children) ? raw.children : [];
    for (const c of children) {
      if (!isObject(c)) continue;
      const child = normalizeNode(c);
      node.children.push(child);
      stack.push([c, child]);
    }
  }
  return out;
}

/** 본문 문자열 → 마인드맵 문서. 소비 경계 → 관용 해석 → 형태 판정 → 절단 → 정규화. */
export function parseMindmap(text: string): MindmapParseResult {
  const b = bounded(text);
  if (b.kind !== "ok") return { ok: false, reason: b.kind, preview: b.preview };
  const parsed = parseLenientJson(b.text);
  if (!parsed.ok) return { ok: false, reason: "invalid_json", detail: "JSON이 아닙니다" };
  const err = validateMindmapShape(parsed.value);
  if (err) return { ok: false, reason: "invalid_shape", detail: err };
  const data = parsed.value as RawNode;
  let root = data.root as RawNode;
  let truncated = data.truncated === true;
  if (countMindmapNodes(root) > MINDMAP_NODE_CAP) {
    const [r, t] = truncateMindmapBfs(root, MINDMAP_NODE_CAP);
    root = r;
    truncated = truncated || t;
  }
  const doc: MindmapDoc = { root: normalizeTree(root), truncated };
  if (typeof data.version === "number") doc.version = data.version;
  return { ok: true, doc };
}

/** 정규화본 JSON(내보내기용) — 라벨은 정화한 전체 라벨. */
export function serializeMindmap(doc: MindmapDoc): string {
  type Out = { id: string; label: string; source_ref?: MindmapSourceRef; children: Out[] };
  const toOut = (n: MindmapNode): Out => {
    const o: Out = { id: n.id, label: n.title, children: [] };
    if (n.source_ref) o.source_ref = { ...n.source_ref };
    return o;
  };
  const root = toOut(doc.root);
  const stack: [MindmapNode, Out][] = [[doc.root, root]];
  while (stack.length > 0) {
    const [n, o] = stack.pop() as [MindmapNode, Out];
    for (const c of n.children) {
      const co = toOut(c);
      o.children.push(co);
      stack.push([c, co]);
    }
  }
  return JSON.stringify(
    { version: doc.version ?? 1, root, truncated: doc.truncated },
    null,
    2,
  );
}
