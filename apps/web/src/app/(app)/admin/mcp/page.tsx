"use client";

import { useMutation } from "@tanstack/react-query";
import { Plus, Server } from "lucide-react";
import { useId, useState } from "react";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { CrudFormDialog } from "@/components/admin/crud-form-dialog";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { RowActions } from "@/components/ui/row-actions";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { useCrudResource } from "@/hooks/use-crud-resource";
import { usePagination } from "@/hooks/use-pagination";
import { api } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { activeLabel, healthLabel, labelFor } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";
import type { AdminMCPServer, MCPTool } from "@/lib/admin-types";
import {
  MCP_TOOL_APPROVAL_LABEL,
  MCP_TOOL_SANITIZED_LABEL,
  mcpToolRejectedLabel,
} from "@/app/(app)/admin/mcp/tool-spec-labels";

const BASE_PATH = "/api/v1/admin/mcp-servers";

const EMPTY_FORM = { name: "", transport: "stdio", endpoint_or_command: "" };

/**
 * 전송 방식 표시 — 프로토콜 이름은 식별자라 번역하지 않고(§6.3.2 프로바이더 유형과 같은 원칙), stdio에만 뜻을 붙인다.
 * 이 화면(목록·추가 폼)에서만 쓰므로 공용 사전(`lib/labels.ts`)에 두지 않는다. 미지 값은 원시값.
 */
const TRANSPORT_LABEL = {
  stdio: "stdio(명령 실행)",
  sse: "SSE",
  streamable_http: "Streamable HTTP",
} as const;

const transportLabel = (transport: string) => labelFor(TRANSPORT_LABEL, transport);

/**
 * 헬스체크 응답 — 생성 스키마 `MCPHealthCheckOut` 모양(core-v0.20.0-upgrade §5.7). 도구 항목 `MCPTool`은 생성 타입
 * `MCPToolHealthOut` 별칭이고, 원문 객체(입력 스키마·annotations·원문 설명)는 응답에 없다.
 */
type MCPHealthCheckResult = { ok: boolean; tools: MCPTool[]; error?: string | null };

/** 승인 고정 상태 → 배지 톤(노출되는 승인만 success, 재승인이 필요하면 warning). */
const APPROVAL_TONE: Record<keyof typeof MCP_TOOL_APPROVAL_LABEL, StatusTone> = {
  approved: "success",
  unpinned: "warning",
  changed: "warning",
  not_approved: "neutral",
};

/**
 * 상태 확인 모달의 도구 한 줄 — 코어가 정화한 설명을 텍스트로만 그리고, 노출 판정 배지를 붙인다(§5.7).
 * `rejected`는 설명이 빈 문자열이라 설명 줄이 없고 「노출 불가: <사유 라벨>」, `sanitized`는 「설명 정화됨」, `ok`는 배지 없음.
 * 승인 고정 상태 배지(코어 v0.21.0 #293)와, 재승인 CLI(`set TOOL=HASH`)에 넘길 현재 스펙 해시를 함께 보인다.
 */
function ToolSpecItem({ tool }: { tool: MCPTool }) {
  const { status, reason } = tool.spec;
  const approval = tool.approval ?? "not_approved";
  return (
    <li
      className="rounded-md border p-2 text-sm"
      data-testid={`mcp-tool-${tool.name}`}
      data-spec-status={status}
      data-approval={approval}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <p className="font-mono font-medium break-all">{tool.name}</p>
        <StatusBadge tone={APPROVAL_TONE[approval]} data-testid="mcp-tool-approval">
          {MCP_TOOL_APPROVAL_LABEL[approval]}
        </StatusBadge>
        {status === "rejected" && (
          <StatusBadge tone="danger" className="h-auto whitespace-normal">
            {mcpToolRejectedLabel(reason)}
          </StatusBadge>
        )}
        {status === "sanitized" && <StatusBadge tone="warning">{MCP_TOOL_SANITIZED_LABEL}</StatusBadge>}
      </div>
      {tool.description && (
        <p className="text-xs whitespace-pre-wrap break-words text-muted-foreground" data-testid="mcp-tool-description">
          {tool.description}
        </p>
      )}
      {tool.spec_hash && (
        <p className="mt-1 font-mono text-[11px] break-all text-muted-foreground select-all" data-testid="mcp-tool-spec-hash">
          {tool.spec_hash}
        </p>
      )}
    </li>
  );
}

/** 헬스 → 배지 톤(정상 success · 오류 danger · 미확인 neutral). 문구는 `healthLabel`(DP-4: 색만으로 전하지 않음). */
function healthTone(ok: boolean | null): StatusTone {
  if (ok === true) return "success";
  if (ok === false) return "danger";
  return "neutral";
}

function useMcpServers() {
  // 실패 토스트는 호출부가 notify 규칙(「…하지 못했습니다」)으로 띄운다.
  return useCrudResource<AdminMCPServer, typeof EMPTY_FORM, { enabled?: boolean }>({
    queryKey: qk.adminMcp.all,
    basePath: BASE_PATH,
    messages: { create: "MCP 서버를 추가했습니다" },
    toastErrors: false,
  });
}

function CreateServerDialog({
  open,
  onOpenChange,
  create,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  create: ReturnType<typeof useMcpServers>["create"];
}) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const nameId = useId();
  const transportId = useId();
  const targetId = useId();

  return (
    <CrudFormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="MCP 서버 추가"
      description="에이전트가 도구를 빌려 쓸 MCP 서버를 등록합니다. 추가한 뒤 「상태 확인」으로 연결을 점검하세요."
      submitLabel={create.isPending ? "추가 중…" : "추가"}
      pending={create.isPending}
      error={error}
      onReset={() => {
        setForm(EMPTY_FORM);
        setError(null);
      }}
      onSubmit={() => {
        setError(null);
        create.mutate(form, {
          onSuccess: () => onOpenChange(false),
          onError: (e) => {
            setError("MCP 서버를 추가하지 못했습니다. 입력값을 확인해 주세요.");
            notifyError("MCP 서버를 추가", e);
          },
        });
      }}
    >
      <Label htmlFor={nameId}>이름</Label>
      <Input
        id={nameId}
        required
        value={form.name}
        onChange={(e) => setForm({ ...form, name: e.target.value })}
      />
      <Label htmlFor={transportId}>전송 방식</Label>
      <Select
        value={form.transport}
        onValueChange={(v) => v && setForm({ ...form, transport: v })}
      >
        <SelectTrigger id={transportId} className="w-full">
          <SelectValue>{(v: string) => transportLabel(v)}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {Object.entries(TRANSPORT_LABEL).map(([value, label]) => (
            <SelectItem key={value} value={value}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Label htmlFor={targetId}>{form.transport === "stdio" ? "실행 명령" : "엔드포인트 URL"}</Label>
      <Input
        id={targetId}
        required
        className="font-mono"
        value={form.endpoint_or_command}
        onChange={(e) => setForm({ ...form, endpoint_or_command: e.target.value })}
        placeholder={
          form.transport === "stdio"
            ? "python -m scripts.demo_mcp_server"
            : "https://mcp.example.com/sse"
        }
      />
    </CrudFormDialog>
  );
}

export default function AdminMCPPage() {
  const servers = useMcpServers();
  const [createOpen, setCreateOpen] = useState(false);
  const [tools, setTools] = useState<{ server: string; tools: MCPTool[] } | null>(null);
  const pg = usePagination(servers.list.data ?? []);

  // 목록 CRUD가 아닌 페이지 고유 동작 — 성공 시 응답 본문(도구 목록)을 모달로 띄운다.
  const healthCheck = useMutation({
    mutationFn: (id: string) =>
      api.post<MCPHealthCheckResult>(`${BASE_PATH}/${id}/health-check`),
    onSuccess: (r, id) => {
      servers.invalidate();
      const name = servers.list.data?.find((s) => s.id === id)?.name ?? "";
      if (r.ok && r.tools) {
        setTools({ server: name, tools: r.tools });
        notifySuccess(`‘${name}’ 서버 상태를 확인했습니다`, {
          description: `정상 — 도구 ${r.tools.length}개`,
        });
      } else {
        notifyError(`‘${name}’ 서버에 연결`, r.error);
      }
    },
    onError: (e) => notifyError("상태를 확인", e),
  });
  const checkingId = healthCheck.isPending ? healthCheck.variables : null;

  let body: React.ReactNode;
  if (servers.list.isPending) {
    body = <LoadingState variant="table" />;
  } else if (servers.list.isError) {
    body = (
      <ErrorState
        title="MCP 서버 목록을 불러오지 못했습니다"
        onRetry={() => servers.list.refetch()}
        retrying={servers.list.isFetching}
      />
    );
  } else if (pg.total === 0) {
    body = (
      <EmptyState
        icon={<Server />}
        title="아직 MCP 서버가 없습니다"
        description="「MCP 서버 추가」로 첫 서버를 등록하세요."
      />
    );
  } else {
    body = (
      <>
        <AdminTableFrame>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>이름</TableHead>
                <TableHead>전송 방식</TableHead>
                <TableHead>엔드포인트/명령</TableHead>
                <TableHead>상태</TableHead>
                <TableHead>활성</TableHead>
                <TableHead className="w-32 text-right">동작</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pg.pageItems.map((s) => (
                <TableRow key={s.id}>
                  <TableCell className="font-medium">{s.name}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{transportLabel(s.transport)}</Badge>
                  </TableCell>
                  <TableCell className="max-w-60 truncate font-mono text-xs" title={s.endpoint_or_command}>
                    {s.endpoint_or_command}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-col items-start gap-0.5">
                      <StatusBadge tone={healthTone(s.last_health_ok)}>
                        {healthLabel(s.last_health_ok)}
                      </StatusBadge>
                      {s.last_health_check && (
                        <span className="text-xs text-muted-foreground">
                          {formatDateTime(s.last_health_check)} 확인
                        </span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={s.enabled}
                        aria-label={`${s.name} 활성`}
                        onCheckedChange={(c) =>
                          servers.update.mutate(
                            { id: s.id, enabled: c },
                            {
                              onError: (e) =>
                                notifyError(`‘${s.name}’ 서버를 ${c ? "활성화" : "비활성화"}`, e),
                            },
                          )
                        }
                      />
                      <span aria-hidden className="text-xs text-muted-foreground">
                        {activeLabel(s.enabled)}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <RowActions
                      foldPrimaryOnNarrow
                      rowLabel={s.name}
                      primary={{
                        label: checkingId === s.id ? "확인 중…" : "상태 확인",
                        onClick: () => healthCheck.mutate(s.id),
                        disabled: healthCheck.isPending,
                      }}
                      items={[]}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </AdminTableFrame>
        <TablePagination pagination={pg} />
      </>
    );
  }

  return (
    <div>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "MCP 서버" }]}
        title="MCP 서버"
        description="에이전트가 사용하는 MCP 서버 연결을 관리합니다"
        actions={
          <Button className="coarse:min-h-11" onClick={() => setCreateOpen(true)}>
            <Plus aria-hidden />
            MCP 서버 추가
          </Button>
        }
      />
      <CreateServerDialog open={createOpen} onOpenChange={setCreateOpen} create={servers.create} />
      {/* 목록 영역 경계 — 「다시 시도」가 성공하면 ErrorState가 이 안의 첫 포커스 대상(표)으로 포커스를 옮긴다(HCI-P2-2) */}
      <div data-slot="mcp-server-list">{body}</div>

      <Dialog open={tools !== null} onOpenChange={(o) => !o && setTools(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>제공 도구 — {tools?.server}</DialogTitle>
            <DialogDescription>
              {tools && tools.tools.length > 0
                ? `이 서버가 제공하는 도구 ${tools.tools.length}개입니다.`
                : "이 서버가 제공하는 도구가 없습니다."}
            </DialogDescription>
          </DialogHeader>
          {tools && tools.tools.length > 0 && (
            <ul className="flex max-h-[60dvh] flex-col gap-2 overflow-y-auto">
              {tools.tools.map((t, i) => (
                // 이름은 서버가 128자로 자른 값이라 겹칠 수 있다 — 순번을 키에 더한다
                <ToolSpecItem key={`${i}:${t.name}`} tool={t} />
              ))}
            </ul>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
