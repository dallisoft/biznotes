"use client";

import { Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  useCreateSource,
  useResolveUploadLimit,
  useSources,
  useUploadLimits,
  useUploadSource,
  useWebSearch,
} from "@/hooks/use-sources";
import { formatMegabytes, sourceLimitFrom } from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";
import {
  filterNewFiles,
  splitOversizedFiles,
  uploadFilesWithConcurrency,
  type UploadResultCounts,
} from "@/lib/file-upload";
import type { WebSearchResult } from "@/lib/source-types";

const ACCEPTED = ".pdf,.docx,.pptx,.xlsx,.txt,.md,.markdown";


const UPLOAD_RESULT_LABELS: Record<keyof UploadResultCounts, string> = {
  existingClientSkipped: "기존 파일 제외",
  sameBatchSkipped: "선택 내 중복 제외",
  oversizedClientSkipped: "크기 초과 제외",
  serverDuplicate409: "서버 중복(409)",
  uploaded: "업로드 성공",
  validationOrCapacity422: "검증·용량 초과(422)",
  tooLarge413: "크기 초과(413)",
  rateLimited429: "요청 제한(429)",
  networkFailed: "네트워크 실패",
};

function uploadSummary(counts: UploadResultCounts): string {
  return (Object.keys(UPLOAD_RESULT_LABELS) as Array<keyof UploadResultCounts>)
    .filter((key) => counts[key] > 0)
    .map((key) => `${UPLOAD_RESULT_LABELS[key]} ${counts[key]}개`)
    .join(" · ");
}

/** 크기를 넘어 보내지 않은 파일 안내 — 이름은 앞 5개까지 보이고 나머지는 개수로 줄인다. */
const OVERSIZED_NAMES_SHOWN = 5;

function oversizedDescription(files: readonly File[], maxBytes: number): string {
  const names = files.slice(0, OVERSIZED_NAMES_SHOWN).map((file) => file.name);
  const rest = files.length - names.length;
  return `최대 ${formatMegabytes(maxBytes)}보다 커서 올리지 않았습니다: ${names.join(", ")}${rest > 0 ? ` 외 ${rest}개` : ""}`;
}

function showUploadSummary(counts: UploadResultCounts, description?: string) {
  const message = uploadSummary(counts);
  if (!message) return;

  const hasFailure =
    counts.oversizedClientSkipped > 0 ||
    counts.serverDuplicate409 > 0 ||
    counts.validationOrCapacity422 > 0 ||
    counts.tooLarge413 > 0 ||
    counts.rateLimited429 > 0 ||
    counts.networkFailed > 0;
  const kind = hasFailure ? "warning" : counts.uploaded > 0 ? "success" : "info";
  // 설명(크기를 넘어 보내지 않은 파일)이 있을 때만 두 번째 인자를 넘긴다
  if (description) toast[kind](message, { description });
  else toast[kind](message);
}

type AddSourceTab = "file" | "web" | "search" | "text";

export function AddSourceDialog({
  notebookId,
  sourceCount,
  open: controlledOpen,
  onOpenChange,
  defaultTab = "file",
  showTrigger = true,
}: {
  notebookId: string;
  sourceCount: number;
  /** 외부에서 열림 상태를 제어할 때 사용 (미지정 시 내부 상태) */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** 열릴 때 활성화할 탭 */
  defaultTab?: AddSourceTab;
  /** 기본 트리거 버튼 노출 여부 */
  showTrigger?: boolean;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const isControlled = controlledOpen !== undefined;
  const open = isControlled ? controlledOpen : internalOpen;
  const setOpen = (next: boolean) => {
    if (!next) uploadAbortController.current?.abort();
    if (!isControlled) setInternalOpen(next);
    onOpenChange?.(next);
  };
  const [tab, setTab] = useState<AddSourceTab>(defaultTab);
  // 열릴 때마다 요청된 기본 탭으로 초기화
  useEffect(() => {
    if (open) setTab(defaultTab);
  }, [open, defaultTab]);
  const create = useCreateSource(notebookId);
  const sources = useSources(notebookId);
  const upload = useUploadSource(notebookId);
  const search = useWebSearch(notebookId);
  // 업로드 최대 크기 — 문구와 사전 검사에 쓴다. 못 읽었으면 사전 검사 없이 보내고 서버 413이 판정한다
  const uploadLimits = useUploadLimits(open);
  const maxUploadBytes = uploadLimits.data?.max_upload_bytes;
  // 노트북당 소스 상한(코어 v0.30.0 #445, 배포값 500 — #281). 서버 판정과 같은 값이다. 못 읽었으면
  // (조회 중·실패·옛 api) 분모 없이 개수만 보이고 서버 422가 판정한다
  const maxSources = sourceLimitFrom(uploadLimits.data);
  const sourcesFull = maxSources !== undefined && sourceCount >= maxSources;
  const resolveUploadLimit = useResolveUploadLimit();

  const fileInput = useRef<HTMLInputElement>(null);
  const uploadAbortController = useRef<AbortController | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [urlValue, setUrlValue] = useState("");
  const [textValue, setTextValue] = useState("");
  const [textTitle, setTextTitle] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [results, setResults] = useState<WebSearchResult[]>([]);
  const [selectedUrls, setSelectedUrls] = useState<Set<string>>(new Set());
  const [addingSelected, setAddingSelected] = useState(false);
  const [uploadingFiles, setUploadingFiles] = useState(false);
  const [checkingLimit, setCheckingLimit] = useState(false);

  async function handleFiles(fileList: FileList | null) {
    const files = fileList ? Array.from(fileList) : [];
    // Reset immediately so selecting the same file again still fires change.
    if (fileInput.current) fileInput.current.value = "";
    if (files.length === 0) return;

    if (sources.isPending && !sources.data) {
      toast.info("기존 소스를 확인하는 중입니다. 잠시 후 다시 시도해 주세요.");
      return;
    }

    const controller = new AbortController();
    uploadAbortController.current = controller;
    const release = () => {
      if (uploadAbortController.current === controller) uploadAbortController.current = null;
    };
    // 처음 조회가 끝나기 전이면 한도를 기다린다 — 초과 파일을 먼저 보내지 않게. 조회에 실패했으면 기다리지 않는다
    let maxBytes = maxUploadBytes;
    if (maxBytes === undefined && !uploadLimits.isError) {
      setCheckingLimit(true);
      maxBytes = await resolveUploadLimit();
      setCheckingLimit(false);
      if (controller.signal.aborted) {
        // 기다리는 동안 다이얼로그를 닫았다 — 보내지 않는다
        release();
        return;
      }
    }

    const selection = filterNewFiles(files, sources.data ?? []);
    // 업로드 최대 크기를 넘는 파일은 보내지 않는다(이름과 한도를 안내한다)
    const sized =
      maxBytes === undefined
        ? { files: selection.files, oversized: [] }
        : splitOversizedFiles(selection.files, maxBytes);
    const counts = { ...selection.counts, oversizedClientSkipped: sized.oversized.length };
    const description =
      sized.oversized.length > 0 && maxBytes !== undefined
        ? oversizedDescription(sized.oversized, maxBytes)
        : undefined;
    if (sized.files.length === 0) {
      release();
      showUploadSummary(counts, description);
      return;
    }

    setUploadingFiles(true);
    const result = await uploadFilesWithConcurrency(
      sized.files,
      (file, signal) => upload.mutateAsync({ file, signal }),
      { initialCounts: counts, signal: controller.signal },
    );
    const aborted = controller.signal.aborted;
    release();
    setUploadingFiles(false);
    if (aborted) return;
    showUploadSummary(result.counts, description);
    setOpen(false);
  }

  function addUrl(type: "web", url: string, title?: string) {
    create.mutate(
      { type, url, title },
      {
        onSuccess: () => {
          toast.success("소스가 등록되었습니다 — 처리 중");
          setUrlValue("");
          setOpen(false);
        },
        onError: (err) => toast.error(`등록 실패: ${err.message}`),
      },
    );
  }

  function addText() {
    create.mutate(
      { type: "text", text: textValue, title: textTitle || undefined },
      {
        onSuccess: () => {
          toast.success("텍스트 소스가 등록되었습니다");
          setTextValue("");
          setTextTitle("");
          setOpen(false);
        },
        onError: (err) => toast.error(`등록 실패: ${err.message}`),
      },
    );
  }

  function runSearch(e: React.FormEvent) {
    e.preventDefault();
    search.mutate(
      { query: searchQuery },
      {
        onSuccess: (data) => {
          setResults(data);
          setSelectedUrls(new Set());
        },
        onError: (err) => toast.error(`검색 실패: ${err.message}`),
      },
    );
  }

  function toggleResult(url: string) {
    setSelectedUrls((prev) => {
      const next = new Set(prev);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  }

  async function addSelected() {
    const picked = results.filter((r) => selectedUrls.has(r.url));
    if (picked.length === 0) return;
    setAddingSelected(true);
    const outcomes = await Promise.allSettled(
      picked.map((r) =>
        create.mutateAsync({
          type: "web",
          url: r.url,
          title: r.title,
        }),
      ),
    );
    setAddingSelected(false);
    const ok = outcomes.filter((o) => o.status === "fulfilled").length;
    const failed = picked.length - ok;
    if (ok > 0) toast.success(`소스 ${ok}개가 등록되었습니다 — 처리 중`);
    if (failed > 0) toast.error(`소스 ${failed}개 등록에 실패했습니다`);
    if (failed === 0) {
      setResults([]);
      setSelectedUrls(new Set());
      setSearchQuery("");
      setOpen(false);
    } else {
      // 실패한 항목만 남겨 재시도할 수 있게 한다
      const okUrls = new Set(
        picked.filter((_, i) => outcomes[i].status === "fulfilled").map((r) => r.url),
      );
      setResults((prev) => prev.filter((r) => !okUrls.has(r.url)));
      setSelectedUrls((prev) => {
        const next = new Set(prev);
        for (const url of okUrls) next.delete(url);
        return next;
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {showTrigger && (
        // 패널 머리글 동작은 outline + Plus로 통일한다(plan §6.6.2 — 「소스 추가」「새 메모」)
        <DialogTrigger
          render={
            <Button
              variant="outline"
              size="sm"
              className="coarse:min-h-11"
              data-testid="add-source"
            />
          }
        >
          <Plus aria-hidden />
          소스 추가
        </DialogTrigger>
      )}
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>소스 추가</DialogTitle>
          <DialogDescription>
            {maxSources === undefined
              ? `소스 ${formatNumber(sourceCount)}개 사용 중`
              : `${formatNumber(sourceCount)}/${formatNumber(maxSources)} 사용 중`}
          </DialogDescription>
        </DialogHeader>
        {sourcesFull && (
          <p className="text-sm text-destructive" role="status" data-testid="sources-full">
            노트북당 소스는 최대 {formatNumber(maxSources)}개입니다. 소스를 지워야 더 추가할 수 있습니다.
          </p>
        )}
        {(checkingLimit || uploadingFiles) && (
          <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
            {checkingLimit ? "업로드 한도를 확인하는 중입니다…" : "파일을 업로드하는 중입니다…"}
          </p>
        )}
        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as AddSourceTab)}
          className="min-w-0"
        >
          <TabsList className="w-full">
            <TabsTrigger value="file">파일</TabsTrigger>
            <TabsTrigger value="web">웹</TabsTrigger>
            <TabsTrigger value="search">웹 검색</TabsTrigger>
            <TabsTrigger value="text">텍스트</TabsTrigger>
          </TabsList>

          <TabsContent value="file">
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={uploadingFiles || checkingLimit}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                handleFiles(e.dataTransfer.files);
              }}
              className={`flex h-40 w-full flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed text-sm text-muted-foreground transition-colors ${
                dragOver ? "border-primary bg-primary/5" : "hover:bg-accent/50"
              }`}
            >
              <span className="text-2xl">📎</span>
              <span>클릭하거나 파일을 끌어다 놓으세요</span>
              <span className="text-xs" data-testid="upload-hint">
                PDF · DOCX · PPTX · XLSX · TXT · MD
                {maxUploadBytes !== undefined && ` (최대 ${formatMegabytes(maxUploadBytes)})`}
              </span>
            </button>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPTED}
              multiple
              hidden
              disabled={uploadingFiles || checkingLimit}
              onChange={(e) => handleFiles(e.target.files)}
            />
          </TabsContent>

          <TabsContent value="web" className="flex flex-col gap-3">
            <Label htmlFor="src-url">URL</Label>
            <Input
              id="src-url"
              placeholder="https://... (웹 페이지)"
              value={urlValue}
              onChange={(e) => setUrlValue(e.target.value)}
            />
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={!urlValue.startsWith("http") || create.isPending}
                onClick={() =>
                  addUrl("web", urlValue)
                }
              >
                등록
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="search" className="flex flex-col gap-3">
            <form onSubmit={runSearch} className="flex gap-2">
              <Input
                placeholder="검색어"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              <Button type="submit" size="sm" disabled={search.isPending || !searchQuery.trim()}>
                {search.isPending ? "검색 중..." : "검색"}
              </Button>
            </form>
            {results.length > 0 && (
              <div className="flex items-center justify-between">
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Checkbox
                    checked={selectedUrls.size === results.length}
                    indeterminate={
                      selectedUrls.size > 0 && selectedUrls.size < results.length
                    }
                    onCheckedChange={(checked) =>
                      setSelectedUrls(
                        checked ? new Set(results.map((r) => r.url)) : new Set(),
                      )
                    }
                  />
                  전체 선택
                </label>
                <span className="text-xs text-muted-foreground">
                  {selectedUrls.size}/{results.length}개 선택됨
                </span>
              </div>
            )}
            <ScrollArea className="h-64">
              <ul className="flex flex-col gap-2 pr-3">
                {results.map((r) => (
                  <li key={r.url}>
                    <label
                      className={`flex w-full cursor-pointer items-start gap-3 overflow-hidden rounded-md border p-2 text-sm transition-colors ${
                        selectedUrls.has(r.url)
                          ? "border-primary bg-primary/5"
                          : "hover:bg-accent/50"
                      }`}
                    >
                      <Checkbox
                        className="mt-0.5"
                        checked={selectedUrls.has(r.url)}
                        onCheckedChange={() => toggleResult(r.url)}
                      />
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{r.title}</p>
                        <p className="truncate text-xs text-muted-foreground">{r.url}</p>
                        {r.snippet && (
                          <p className="line-clamp-2 text-xs text-muted-foreground">
                            {r.snippet}
                          </p>
                        )}
                      </div>
                    </label>
                  </li>
                ))}
                {results.length === 0 && (
                  <li className="py-8 text-center text-sm text-muted-foreground">
                    {search.isPending ? "검색 중..." : "검색 결과가 여기에 표시됩니다"}
                  </li>
                )}
              </ul>
            </ScrollArea>
            {results.length > 0 && (
              <Button
                size="sm"
                className="self-end"
                disabled={selectedUrls.size === 0 || addingSelected}
                onClick={addSelected}
              >
                {addingSelected
                  ? "추가 중..."
                  : `선택한 소스 추가 (${selectedUrls.size})`}
              </Button>
            )}
          </TabsContent>

          <TabsContent value="text" className="flex flex-col gap-3">
            <Input
              placeholder="제목 (선택)"
              value={textTitle}
              onChange={(e) => setTextTitle(e.target.value)}
            />
            <Textarea
              placeholder="텍스트를 붙여넣으세요"
              rows={8}
              value={textValue}
              onChange={(e) => setTextValue(e.target.value)}
            />
            <Button
              size="sm"
              className="self-end"
              disabled={!textValue.trim() || create.isPending}
              onClick={addText}
            >
              등록
            </Button>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
