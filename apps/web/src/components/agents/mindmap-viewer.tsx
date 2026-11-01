"use client";

import {
  Background,
  Handle,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ChevronLeft,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  FileJson,
  FileSearch,
  ImageDown,
  Maximize,
  Maximize2,
  Minimize2,
  Minus,
  Plus,
} from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSources } from "@/hooks/use-sources";
import type { AgentRun } from "@/lib/agent-types";
import { boundedPrettyJson } from "@/lib/mindmap/bounded";
import { discussPrompt } from "@/lib/mindmap/discuss-prompt";
import { exportMindmapJson, exportMindmapPng } from "@/lib/mindmap/export";
import { allExpandableIds, layoutMindmap, type LayoutNode } from "@/lib/mindmap/layout";
import {
  parseMindmap,
  type MindmapDoc,
  type MindmapNode,
  type MindmapParseFailure,
} from "@/lib/mindmap/parse";
import { useChatPrompt } from "@/stores/chat-prompt";
import { useSourceFocus } from "@/stores/source-focus";

/** 같은 노드를 이 시간 안에 다시 누르면 채팅 요청을 보내지 않는다(펼침만 — T5). */
export const DISCUSS_DEBOUNCE_MS = 300;

const DEPTH_BG = [
  "bg-[var(--mindmap-depth-0)]",
  "bg-[var(--mindmap-depth-1)]",
  "bg-[var(--mindmap-depth-2)]",
];

interface CanvasNodeData extends Record<string, unknown> {
  layout: LayoutNode;
  tabbable: boolean;
  showSourceButton: boolean;
  onToggle: (id: string) => void;
  onActivate: (id: string) => void;
  onFocusSource: (sourceId: string) => void;
  onFocusNode: (id: string) => void;
}

export function nodeAriaLabel(n: LayoutNode): string {
  const base = `${n.node.title}, ${n.depth + 1}단계, 하위 ${n.node.children.length}개`;
  if (!n.hasChildren) return base;
  return `${base}, ${n.expanded ? "펼쳐짐" : "접힘"}`;
}

function MindmapCanvasNode({ data }: NodeProps) {
  const d = data as CanvasNodeData;
  const n = d.layout;
  return (
    <div
      // React Flow는 선택·드래그·연결이 모두 꺼진 노드의 래퍼에 pointer-events:none을 준다 — 노드 안 버튼이
      // 클릭을 받도록 되살리고(nopan·nodrag: 버튼 위 누름이 캔버스 팬으로 새지 않게) 한다.
      className={`nopan nodrag pointer-events-auto flex h-full w-full items-center gap-1 rounded-lg border px-2 text-foreground shadow-sm ${DEPTH_BG[Math.min(n.depth, 2)]}`}
      data-testid="mindmap-node"
      data-depth={n.depth}
    >
      <Handle type="target" position={Position.Left} className="!opacity-0" isConnectable={false} />
      <button
        type="button"
        data-mindmap-node-id={n.id}
        tabIndex={d.tabbable ? 0 : -1}
        aria-label={nodeAriaLabel(n)}
        onClick={() => d.onActivate(n.id)}
        onFocus={() => d.onFocusNode(n.id)}
        className="line-clamp-3 min-w-0 flex-1 rounded-sm text-left text-sm font-medium break-words outline-none focus-visible:ring-2 focus-visible:ring-ring"
        title={n.node.title}
      >
        {n.node.label}
      </button>
      {d.showSourceButton && n.node.source_ref && (
        <button
          type="button"
          tabIndex={-1}
          aria-label="소스로 이동"
          title="소스로 이동"
          onClick={() => d.onFocusSource((n.node.source_ref as { source_id: string }).source_id)}
          className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
          data-testid="mindmap-node-source"
        >
          <FileSearch className="size-3.5" />
        </button>
      )}
      {n.hasChildren && (
        <button
          type="button"
          tabIndex={-1}
          onClick={() => d.onToggle(n.id)}
          className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
          data-testid="mindmap-node-toggle"
          aria-expanded={n.expanded}
          aria-label={n.expanded ? "접기" : "펼치기"}
        >
          {n.expanded ? <ChevronLeft className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </button>
      )}
      <Handle type="source" position={Position.Right} className="!opacity-0" isConnectable={false} />
    </div>
  );
}

const MINDMAP_NODE_TYPES = { mindmap: MindmapCanvasNode };

type FullscreenMode = "none" | "native" | "overlay";

function indexTree(root: MindmapNode): {
  byId: Map<string, MindmapNode>;
  parentOf: Map<string, string | null>;
} {
  const byId = new Map<string, MindmapNode>();
  const parentOf = new Map<string, string | null>();
  const stack: [MindmapNode, string | null][] = [[root, null]];
  while (stack.length > 0) {
    const [n, p] = stack.pop() as [MindmapNode, string | null];
    byId.set(n.id, n);
    parentOf.set(n.id, p);
    for (const c of n.children) stack.push([c, n.id]);
  }
  return { byId, parentOf };
}

function SourcesButton({ run, notebookId }: { run: AgentRun; notebookId: string }) {
  const sources = useSources(notebookId);
  const requestFocus = useSourceFocus((s) => s.request);
  const ids = run.input_source_ids ?? null;
  const items = ids
    ? ids.map((id) => ({ id, title: sources.data?.find((s) => s.id === id)?.title ?? null }))
    : (sources.data ?? []).map((s) => ({ id: s.id, title: s.title as string | null }));
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button variant="outline" size="sm" className="h-7 text-xs" data-testid="mindmap-sources">
            {ids ? `소스 ${ids.length}개 보기` : "전체 소스"}
          </Button>
        }
      />
      <PopoverContent className="w-72 text-sm" data-testid="mindmap-sources-list">
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">소스가 없습니다</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {items.map((item) => (
              <li key={item.id}>
                {item.title === null ? (
                  <span className="block px-1.5 py-1 text-xs text-muted-foreground">삭제된 소스</span>
                ) : (
                  <button
                    type="button"
                    className="block w-full truncate rounded px-1.5 py-1 text-left text-xs hover:bg-accent"
                    onClick={() => requestFocus(item.id)}
                  >
                    {item.title}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  );
}

function MindmapCanvas({
  doc,
  run,
  notebookId,
}: {
  doc: MindmapDoc;
  run: AgentRun;
  notebookId: string;
}) {
  const flow = useReactFlow();
  const hintId = useId();
  const root = doc.root;
  const { byId, parentOf } = useMemo(() => indexTree(root), [root]);
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(root.children.length > 0 ? [root.id] : []),
  );
  const [focusedId, setFocusedId] = useState<string>(root.id);
  const [announce, setAnnounce] = useState("");
  const [fullscreen, setFullscreen] = useState<FullscreenMode>("none");
  const wrapperRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const fullscreenButtonRef = useRef<HTMLButtonElement>(null);
  const pendingFocusRef = useRef(false);
  const lastDiscussRef = useRef<{ id: string; at: number } | null>(null);
  const requestSourceFocus = useSourceFocus((s) => s.request);
  const sources = useSources(notebookId);
  const knownSourceIds = useMemo(
    () => new Set((sources.data ?? []).map((s) => s.id)),
    [sources.data],
  );

  const layout = useMemo(() => layoutMindmap(root, expanded), [root, expanded]);
  const layoutById = useMemo(() => new Map(layout.nodes.map((n) => [n.id, n])), [layout]);

  // 접힘으로 포커스 노드가 사라지면 가장 가까운 보이는 조상으로 옮긴다
  let effectiveFocus = focusedId;
  while (!layoutById.has(effectiveFocus)) {
    const p = parentOf.get(effectiveFocus);
    if (p === undefined || p === null) {
      effectiveFocus = root.id;
      break;
    }
    effectiveFocus = p;
  }

  const setExpandedFor = useCallback((id: string, open: boolean) => {
    setExpanded((prev) => {
      if (prev.has(id) === open) return prev;
      const next = new Set(prev);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const toggle = useCallback(
    (id: string) => setExpandedFor(id, !expanded.has(id)),
    [expanded, setExpandedFor],
  );

  const exitFullscreen = useCallback(() => {
    setFullscreen((mode) => {
      if (mode === "native" && typeof document !== "undefined" && document.fullscreenElement) {
        void document.exitFullscreen?.().catch(() => undefined);
      }
      return "none";
    });
    setTimeout(() => fullscreenButtonRef.current?.focus(), 0);
  }, []);

  async function enterFullscreen() {
    const el = wrapperRef.current;
    if (el && typeof el.requestFullscreen === "function") {
      try {
        await el.requestFullscreen();
        setFullscreen("native");
        return;
      } catch {
        // 거부 — 고정 오버레이로 대체
      }
    }
    setFullscreen("overlay");
  }

  useEffect(() => {
    if (fullscreen !== "native") return;
    const onChange = () => {
      if (!document.fullscreenElement) {
        setFullscreen("none");
        fullscreenButtonRef.current?.focus();
      }
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [fullscreen]);

  useEffect(() => {
    if (fullscreen !== "overlay") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") exitFullscreen();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen, exitFullscreen]);

  /** 라벨 클릭·Enter — 접힌 자식 있는 노드는 펼치고(접지 않는다), 채팅에 질문을 1회 요청한다(AC-2.3). */
  const activate = useCallback(
    (id: string) => {
      const node = byId.get(id);
      if (!node) return;
      setFocusedId(id);
      if (node.children.length > 0) setExpandedFor(id, true);
      const now = Date.now();
      const last = lastDiscussRef.current;
      if (last && last.id === id && now - last.at < DISCUSS_DEBOUNCE_MS) return;
      lastDiscussRef.current = { id, at: now };
      const store = useChatPrompt.getState();
      if (store.busyNonce !== null || store.hasPending(notebookId)) {
        setAnnounce("답변이 끝난 뒤 다시 눌러 주세요");
        return;
      }
      if (fullscreen !== "none") exitFullscreen();
      const parentId = parentOf.get(id) ?? null;
      const parent = parentId ? byId.get(parentId) : undefined;
      const nonce = store.request({
        notebookId,
        text: discussPrompt(node.title, parent ? parent.title : null),
        sourceIds: run.input_source_ids ?? null,
        origin: "mindmap",
      });
      setAnnounce(nonce ? `「${node.label}」 질문을 채팅에 보냈습니다` : "답변이 끝난 뒤 다시 눌러 주세요");
    },
    [byId, parentOf, notebookId, run.input_source_ids, fullscreen, exitFullscreen, setExpandedFor],
  );

  const focusNode = useCallback((id: string) => setFocusedId(id), []);

  const nodes: Node[] = useMemo(
    () =>
      layout.nodes.map((n) => ({
        id: n.id,
        type: "mindmap",
        position: { x: n.x, y: n.y },
        width: n.width,
        height: n.height,
        style: { width: n.width, height: n.height },
        draggable: false,
        connectable: false,
        selectable: false,
        focusable: false,
        data: {
          layout: n,
          tabbable: n.id === effectiveFocus,
          showSourceButton: Boolean(
            n.node.source_ref && knownSourceIds.has(n.node.source_ref.source_id),
          ),
          onToggle: toggle,
          onActivate: activate,
          onFocusSource: requestSourceFocus,
          onFocusNode: focusNode,
        } satisfies CanvasNodeData,
      })),
    [layout, effectiveFocus, knownSourceIds, toggle, activate, requestSourceFocus, focusNode],
  );

  const edges: Edge[] = useMemo(
    () =>
      layout.edges.map((e) => ({
        ...e,
        type: "default",
        focusable: false,
        selectable: false,
        style: { stroke: "var(--muted-foreground)", strokeWidth: 1.5 },
      })),
    [layout],
  );

  // 키보드 이동 뒤 포커스를 그 노드 버튼으로 옮기고, 화면 밖이면 가운데로 데려온다
  useEffect(() => {
    if (!pendingFocusRef.current) return;
    pendingFocusRef.current = false;
    const buttons =
      canvasRef.current?.querySelectorAll<HTMLButtonElement>("[data-mindmap-node-id]") ?? [];
    const target = Array.from(buttons).find((b) => b.dataset.mindmapNodeId === effectiveFocus);
    target?.focus();
    const n = layoutById.get(effectiveFocus);
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!n || !rect) return;
    const { x: vx, y: vy, zoom } = flow.getViewport();
    const sx = n.x * zoom + vx;
    const sy = n.y * zoom + vy;
    const outside =
      sx < 0 || sy < 0 || sx + n.width * zoom > rect.width || sy + n.height * zoom > rect.height;
    if (outside) void flow.setCenter(n.x + n.width / 2, n.y + n.height / 2, { zoom });
  }, [effectiveFocus, layoutById, flow]);

  function moveFocus(id: string | undefined) {
    if (!id) return;
    pendingFocusRef.current = true;
    setFocusedId(id);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const target = e.target as HTMLElement;
    if (!target.dataset?.mindmapNodeId) return;
    const order = layout.nodes;
    const idx = order.findIndex((n) => n.id === effectiveFocus);
    const cur = order[idx];
    if (!cur) return;
    let handled = true;
    switch (e.key) {
      case "ArrowDown":
        moveFocus(order[idx + 1]?.id);
        break;
      case "ArrowUp":
        moveFocus(order[idx - 1]?.id);
        break;
      case "Home":
        moveFocus(order[0]?.id);
        break;
      case "End":
        moveFocus(order[order.length - 1]?.id);
        break;
      case "ArrowRight":
        if (cur.hasChildren && !cur.expanded) setExpandedFor(cur.id, true);
        else if (cur.hasChildren) moveFocus(cur.node.children[0]?.id);
        break;
      case "ArrowLeft":
        if (cur.hasChildren && cur.expanded) setExpandedFor(cur.id, false);
        else moveFocus(cur.parentId ?? undefined);
        break;
      case " ":
        if (cur.hasChildren) setExpandedFor(cur.id, !cur.expanded);
        break;
      case "Enter":
        activate(cur.id);
        break;
      default:
        handled = false;
    }
    if (handled) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  const title = root.title;
  const overlay = fullscreen === "overlay";
  const expandedView = fullscreen !== "none";

  return (
    <div
      ref={wrapperRef}
      className={
        overlay
          ? "fixed inset-0 z-50 flex flex-col gap-2 bg-background p-3"
          : expandedView
            ? "flex h-full flex-col gap-2 bg-background p-3"
            : "flex flex-col gap-2"
      }
      data-testid="mindmap-viewer"
      data-fullscreen={fullscreen}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <h3 className="mr-auto min-w-0 truncate text-sm font-semibold" title={title}>
          {root.label}
        </h3>
        <SourcesButton run={run} notebookId={notebookId} />
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={() => exportMindmapJson(doc, title)}
          data-testid="mindmap-download-json"
        >
          <FileJson className="size-3.5" /> JSON
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={() => void exportMindmapPng(layout, title)}
          data-testid="mindmap-download-png"
        >
          <ImageDown className="size-3.5" /> PNG
        </Button>
        <Button
          ref={fullscreenButtonRef}
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          aria-label={expandedView ? "전체화면 끝내기" : "전체화면"}
          onClick={() => (expandedView ? exitFullscreen() : void enterFullscreen())}
          data-testid="mindmap-fullscreen"
        >
          {expandedView ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
        </Button>
      </div>
      <div
        ref={canvasRef}
        role="group"
        aria-roledescription="마인드맵"
        aria-label={title}
        aria-describedby={hintId}
        onKeyDown={onKeyDown}
        className={`${expandedView ? "min-h-0 flex-1" : "h-[55vh]"} w-full rounded-md border`}
        data-testid="mindmap-canvas"
      >
        <p id={hintId} className="sr-only">
          위·아래 화살표로 항목을 옮기고, 오른쪽·왼쪽 화살표로 펼치거나 접습니다. 스페이스는 펼침을 바꾸고, 엔터는 채팅에서 그
          항목을 묻습니다.
        </p>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={MINDMAP_NODE_TYPES}
          fitView
          minZoom={0.1}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Panel position="bottom-right" className="flex gap-1">
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="전체 펼치기"
              title="전체 펼치기"
              onClick={() => {
                setExpanded(new Set(allExpandableIds(root)));
                setTimeout(() => void flow.fitView(), 0);
              }}
              data-testid="mindmap-expand-all"
            >
              <ChevronsUpDown />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="전체 접기"
              title="전체 접기"
              onClick={() => {
                setExpanded(new Set());
                setTimeout(() => void flow.fitView(), 0);
              }}
              data-testid="mindmap-collapse-all"
            >
              <ChevronsDownUp />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="확대"
              title="확대"
              onClick={() => void flow.zoomIn()}
              data-testid="mindmap-zoom-in"
            >
              <Plus />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="축소"
              title="축소"
              onClick={() => void flow.zoomOut()}
              data-testid="mindmap-zoom-out"
            >
              <Minus />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="화면 맞춤"
              title="화면 맞춤"
              onClick={() => void flow.fitView()}
              data-testid="mindmap-fit"
            >
              <Maximize />
            </Button>
          </Panel>
        </ReactFlow>
      </div>
      {doc.truncated && (
        <p className="rounded-md bg-warning/10 px-3 py-1.5 text-xs text-warning-text">
          ⚠️ 트리가 커서 일부 하위 항목이 생략되었습니다
        </p>
      )}
      <p className="sr-only" aria-live="polite" data-testid="mindmap-announce">
        {announce}
      </p>
    </div>
  );
}

export function MindmapParseError({
  result,
  content,
}: {
  result: MindmapParseFailure;
  content: string;
}) {
  const [showRaw, setShowRaw] = useState(false);
  const rejected = result.reason === "too_large" || result.reason === "too_deep";
  return (
    <div className="flex flex-col gap-2" data-testid="mindmap-parse-error">
      <p className="text-sm text-destructive">
        {rejected
          ? "마인드맵 데이터가 너무 커서 열지 않았습니다"
          : "마인드맵 데이터를 해석하지 못했습니다"}
      </p>
      <Button
        variant="outline"
        size="sm"
        className="self-start"
        onClick={() => setShowRaw((v) => !v)}
        aria-expanded={showRaw}
        data-testid="mindmap-show-raw"
      >
        {showRaw ? "원본 닫기" : "원본 보기"}
      </Button>
      {showRaw && (
        <pre
          className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border p-2 text-xs"
          data-testid="mindmap-raw"
        >
          {/* 상한·깊이 초과로 거부된 본문은 JSON.parse 없이 앞부분 미리보기만 보인다(AC-1.8) */}
          {result.ok === false && "preview" in result ? result.preview : boundedPrettyJson(content)}
        </pre>
      )}
    </div>
  );
}

export function MindmapViewer({
  content,
  run,
  notebookId,
}: {
  content: string;
  run: AgentRun;
  notebookId: string;
}) {
  const parsed = useMemo(() => parseMindmap(content), [content]);
  if (!parsed.ok) return <MindmapParseError result={parsed} content={content} />;
  return (
    <ReactFlowProvider>
      <MindmapCanvas doc={parsed.doc} run={run} notebookId={notebookId} />
    </ReactFlowProvider>
  );
}
