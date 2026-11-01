"use client";

import {
  Blocks,
  FileText,
  House,
  Loader2,
  NotebookText,
  ScanSearch,
  Search,
  StickyNote,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  type ChunkHit,
  useGlobalSearch,
  useSemanticSearch,
} from "@/hooks/use-global-search";
import { useNotebooks } from "@/hooks/use-notebooks";
import { useCommandPalette } from "@/stores/command-palette";

/** 검색어 없이 열었을 때 보이는 최근 노트북 수(ui-ux-improvements ⑦). */
const RECENT_LIMIT = 5;

/** 검색어 없이 열었을 때의 바로 가기. */
const NAV_ITEMS: { key: string; label: string; href: string; icon: typeof NotebookText }[] = [
  { key: "nav-home", label: "노트북 홈", href: "/", icon: House },
  { key: "nav-skills", label: "스킬", href: "/skills", icon: Blocks },
];

interface PaletteItem {
  key: string;
  notebookId: string;
  title: string;
  group: "노트북" | "소스" | "메모" | "최근 노트북";
  icon: typeof NotebookText;
  snippet?: string;
}

type Row =
  | { kind: "item"; key: string; item: PaletteItem }
  | { kind: "semantic-action"; key: string }
  | { kind: "nav"; key: string; label: string; href: string; icon: typeof NotebookText }
  | { kind: "chunk"; key: string; hit: ChunkHit };

export function CommandPalette() {
  const router = useRouter();
  const { open, setOpen } = useCommandPalette();
  const [input, setInput] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  // 시맨틱(본문) 검색은 비용이 있어 명시적 트리거 시에만 — 트리거된 질의를 고정
  const [semanticQuery, setSemanticQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const { query, results } = useGlobalSearch(input);
  const semantic = useSemanticSearch(semanticQuery);
  const notebooks = useNotebooks("all");

  // Cmd+K / Ctrl+K 전역 단축키
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen(!useCommandPalette.getState().open);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [setOpen]);

  // 닫힐 때 입력 초기화
  useEffect(() => {
    if (!open) {
      setInput("");
      setActiveIndex(0);
      setSemanticQuery("");
    }
  }, [open]);

  // 입력이 바뀌면 이전 시맨틱 결과는 무효
  useEffect(() => {
    if (query !== semanticQuery) setSemanticQuery("");
    // 정당: semanticQuery를 deps에 넣으면 자기 자신을 초기화하는 루프가 된다.
    // query 변경에만 반응해야 한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const items = useMemo<PaletteItem[]>(() => {
    const d = results.data;
    if (!d || query.length < 1) return [];
    return [
      ...d.notebooks.map((n) => ({
        key: `nb-${n.id}`,
        notebookId: n.id,
        title: `${n.emoji} ${n.title}`,
        group: "노트북" as const,
        icon: NotebookText,
      })),
      ...d.sources.map((s) => ({
        key: `src-${s.id}`,
        notebookId: s.notebook_id,
        title: s.title,
        group: "소스" as const,
        icon: FileText,
      })),
      ...d.notes.map((n) => ({
        key: `note-${n.id}`,
        notebookId: n.notebook_id,
        title: n.title,
        group: "메모" as const,
        icon: StickyNote,
        snippet: n.snippet,
      })),
    ];
  }, [results.data, query]);

  // 검색어가 없으면 최근 노트북(수정 시각 최신순)과 바로 가기를 보인다 — 빈 입력창만 있던 화면(⑦)
  const emptyRows = useMemo<Row[]>(() => {
    const recent = [...(notebooks.data ?? [])]
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .slice(0, RECENT_LIMIT)
      .map<Row>((n) => ({
        kind: "item",
        key: `recent-${n.id}`,
        item: {
          key: `recent-${n.id}`,
          notebookId: n.id,
          title: `${n.emoji} ${n.title}`,
          group: "최근 노트북",
          icon: NotebookText,
        },
      }));
    return [...recent, ...NAV_ITEMS.map<Row>((nav) => ({ kind: "nav", ...nav }))];
  }, [notebooks.data]);

  const rows = useMemo<Row[]>(() => {
    if (query.length < 1) return emptyRows;
    const r: Row[] = items.map((item) => ({ kind: "item", key: item.key, item }));
    if (query.length >= 2) {
      if (semanticQuery === query) {
        for (const hit of semantic.data?.chunks ?? []) {
          r.push({ kind: "chunk", key: `chunk-${hit.chunk_id}`, hit });
        }
      } else {
        r.push({ kind: "semantic-action", key: "semantic-action" });
      }
    }
    return r;
  }, [items, query, semanticQuery, semantic.data, emptyRows]);

  useEffect(() => setActiveIndex(0), [rows.length, query]);

  function activate(row: Row) {
    if (row.kind === "semantic-action") {
      setSemanticQuery(query);
      return;
    }
    if (row.kind === "nav") {
      setOpen(false);
      router.push(row.href);
      return;
    }
    const notebookId = row.kind === "item" ? row.item.notebookId : row.hit.notebook_id;
    setOpen(false);
    router.push(`/notebooks/${notebookId}`);
  }

  function onInputKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, rows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && rows[activeIndex]) {
      e.preventDefault();
      activate(rows[activeIndex]);
    }
  }

  const searching = query.length >= 1;
  let lastGroup: string | null = null;

  function groupOf(row: Row): string {
    if (row.kind === "item") return row.item.group;
    if (row.kind === "nav") return "바로 가기";
    return "본문";
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        showCloseButton={false}
        className="top-[20%] translate-y-0 gap-0 overflow-hidden p-0 sm:max-w-lg"
      >
        <DialogTitle className="sr-only">전체 검색</DialogTitle>
        <div className="flex items-center gap-2 border-b px-3">
          {results.isFetching || semantic.isFetching ? (
            <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
          ) : (
            <Search className="size-4 shrink-0 text-muted-foreground" />
          )}
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onInputKeyDown}
            placeholder="노트북·소스·메모 검색…"
            className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            data-testid="global-search-input"
            autoFocus
          />
          <kbd className="pointer-events-none shrink-0 rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            ESC
          </kbd>
        </div>
        {(searching || rows.length > 0) && (
          <div
            className="max-h-80 overflow-y-auto p-2"
            data-testid="global-search-results"
          >
            {searching && results.isPending ? (
              <p className="p-3 text-sm text-muted-foreground">검색 중…</p>
            ) : rows.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">결과가 없습니다</p>
            ) : (
              rows.map((row, idx) => {
                const group = groupOf(row);
                const header =
                  group !== lastGroup ? (
                    <p className="px-2 pt-2 pb-1 text-xs font-medium text-muted-foreground">
                      {group}
                    </p>
                  ) : null;
                lastGroup = group;
                const active = idx === activeIndex;
                return (
                  <div key={row.key}>
                    {header}
                    {row.kind === "semantic-action" ? (
                      <button
                        type="button"
                        onClick={() => activate(row)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        data-testid="semantic-search-action"
                        className={`flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm ${
                          active ? "bg-accent text-accent-foreground" : ""
                        }`}
                      >
                        <ScanSearch className="size-4 shrink-0 text-primary" />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate">
                            본문에서 의미 검색: &ldquo;{query}&rdquo;
                          </span>
                          <span className="block truncate text-xs text-muted-foreground">
                            소스 내용을 의미 기반으로 찾습니다
                          </span>
                        </span>
                      </button>
                    ) : row.kind === "nav" ? (
                      <button
                        type="button"
                        onClick={() => activate(row)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm ${
                          active ? "bg-accent text-accent-foreground" : ""
                        }`}
                      >
                        <row.icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="min-w-0 flex-1 truncate">{row.label}</span>
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => activate(row)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        data-row-kind={row.kind === "item" && row.item.group === "최근 노트북" ? "recent" : undefined}
                        className={`flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm ${
                          active ? "bg-accent text-accent-foreground" : ""
                        }`}
                      >
                        {row.kind === "item" ? (
                          <row.item.icon className="size-4 shrink-0 text-muted-foreground" />
                        ) : (
                          <ScanSearch className="size-4 shrink-0 text-muted-foreground" />
                        )}
                        <span className="min-w-0 flex-1">
                          <span className="block truncate">
                            {row.kind === "item" ? row.item.title : row.hit.source_title}
                          </span>
                          {(row.kind === "item" ? row.item.snippet : row.hit.snippet) && (
                            <span className="block truncate text-xs text-muted-foreground">
                              {row.kind === "item" ? row.item.snippet : row.hit.snippet}
                            </span>
                          )}
                        </span>
                      </button>
                    )}
                  </div>
                );
              })
            )}
            {semanticQuery === query &&
              query.length >= 2 &&
              !semantic.isFetching &&
              (semantic.data?.chunks ?? []).length === 0 && (
                <p className="p-3 text-sm text-muted-foreground">본문 매치 없음</p>
              )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
