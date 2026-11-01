"use client";

import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useMemo, useState } from "react";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { Toolbar } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { usePagination } from "@/hooks/use-pagination";
import { api } from "@/lib/api-client";
import { EMPTY, formatDateTime, formatNumber } from "@/lib/format";
import { AUDIT_ACTION_LABEL, auditActionLabel } from "@/lib/labels";
import type { AuditLogRow } from "@/lib/admin-types";

/** 감사 로그는 서버가 최신순 최대 이 건수까지만 준다 — 필터는 받은 건 안에서만 동작한다(§6.6.2, AC-5.7). */
const AUDIT_LIMIT = 100;

/** Select의 「전체」 값 — 빈 문자열은 Base UI Select가 「선택 없음」으로 다루므로 센티널을 쓴다. */
const ALL = "__all__";
/** 사용자 정보가 없는 감사 기록(삭제된 계정·시스템 작업) */
const NO_USER = "__no_user__";
const NO_USER_LABEL = "사용자 정보 없음";

const hasLabel = (code: string) => Object.prototype.hasOwnProperty.call(AUDIT_ACTION_LABEL, code);

function AuditActionCell({ code }: { code: string }) {
  // 알려진 코드 = 라벨 + 원시 코드(작은 모노, 추적성 — §6.3.2). 미지 코드는 원시 코드만 그대로 보인다.
  if (!code) return <span className="text-muted-foreground">{EMPTY}</span>;
  if (!hasLabel(code)) return <code className="font-mono text-xs">{code}</code>;
  return (
    <div className="flex flex-col">
      <span>{auditActionLabel(code)}</span>
      <code className="font-mono text-xs text-muted-foreground">{code}</code>
    </div>
  );
}

/** 관리 › 감사 로그(#236) — 「사용량·감사」 화면의 감사 로그 섹션을 그대로 옮긴 별도 화면이다. */
export default function AdminAuditPage() {
  const audit = useQuery({
    queryKey: ["admin-audit"],
    queryFn: () => api.get<AuditLogRow[]>(`/api/v1/admin/audit-logs?limit=${AUDIT_LIMIT}`),
  });
  const [userFilter, setUserFilter] = useState<string>(ALL);
  const [actionFilter, setActionFilter] = useState<string>(ALL);

  const logs = useMemo(() => audit.data ?? [], [audit.data]);
  // 필터 선택지는 받은 기록에 실제로 있는 값만(§6.6.2 — 받은 100건 안 클라이언트 필터)
  const userOptions = useMemo(() => {
    const set = new Set(logs.map((l) => l.user_email ?? NO_USER));
    return [...set].sort((a, b) => (a === NO_USER ? 1 : b === NO_USER ? -1 : a.localeCompare(b)));
  }, [logs]);
  const actionOptions = useMemo(() => {
    const set = new Set(logs.map((l) => l.action));
    return [...set].sort((a, b) => auditActionLabel(a).localeCompare(auditActionLabel(b), "ko"));
  }, [logs]);
  const filtered = useMemo(
    () =>
      logs.filter(
        (l) =>
          (userFilter === ALL || (l.user_email ?? NO_USER) === userFilter) &&
          (actionFilter === ALL || l.action === actionFilter),
      ),
    [logs, userFilter, actionFilter],
  );
  const pg = usePagination(filtered);
  const filtering = userFilter !== ALL || actionFilter !== ALL;

  function clearFilters() {
    setUserFilter(ALL);
    setActionFilter(ALL);
    pg.setPage(1);
  }

  const userLabel = (v: string) => (v === ALL ? "모든 사용자" : v === NO_USER ? NO_USER_LABEL : v);
  const actionLabel = (v: string) => (v === ALL ? "모든 액션" : auditActionLabel(v));

  let body: React.ReactNode;
  if (audit.isPending) {
    body = <LoadingState variant="table" />;
  } else if (audit.isError) {
    body = (
      <ErrorState
        title="감사 로그를 불러오지 못했습니다"
        onRetry={() => audit.refetch()}
        retrying={audit.isFetching}
      />
    );
  } else if (logs.length === 0) {
    body = (
      <EmptyState
        icon={<ScrollText />}
        title="아직 감사 기록이 없습니다"
        description="관리 작업(설정 변경·사용자 관리 등)을 하면 여기에 기록됩니다."
      />
    );
  } else {
    body = (
      <>
        <Toolbar>
          <Select
            value={userFilter}
            onValueChange={(v) => {
              setUserFilter(v ?? ALL);
              pg.setPage(1);
            }}
          >
            <SelectTrigger aria-label="사용자 필터" className="min-w-44 coarse:min-h-11">
              <SelectValue>{(v: string) => userLabel(v)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>{userLabel(ALL)}</SelectItem>
              {userOptions.map((u) => (
                <SelectItem key={u} value={u}>
                  {userLabel(u)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={actionFilter}
            onValueChange={(v) => {
              setActionFilter(v ?? ALL);
              pg.setPage(1);
            }}
          >
            <SelectTrigger aria-label="액션 필터" className="min-w-44 coarse:min-h-11">
              <SelectValue>{(v: string) => actionLabel(v)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>{actionLabel(ALL)}</SelectItem>
              {actionOptions.map((a) => (
                <SelectItem key={a} value={a}>
                  {actionLabel(a)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-sm text-muted-foreground" role="status" data-testid="audit-count">
            {filtering
              ? `받은 ${formatNumber(logs.length)}건 중 ${formatNumber(filtered.length)}건`
              : `${formatNumber(logs.length)}건`}
          </span>
        </Toolbar>
        {filtered.length === 0 ? (
          <FilteredEmptyState onClearFilters={clearFilters} />
        ) : (
          <>
            <AdminTableFrame>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>시각</TableHead>
                    <TableHead>사용자</TableHead>
                    <TableHead>액션</TableHead>
                    <TableHead>대상</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pg.pageItems.map((log) => (
                    <TableRow key={log.id}>
                      <TableCell className="text-xs whitespace-nowrap tabular-nums">
                        {formatDateTime(log.created_at, { seconds: true })}
                      </TableCell>
                      <TableCell>{log.user_email ?? <span className="text-muted-foreground">{EMPTY}</span>}</TableCell>
                      <TableCell>
                        <AuditActionCell code={log.action} />
                      </TableCell>
                      <TableCell className="max-w-60 truncate text-xs" title={log.resource || undefined}>
                        {log.resource || <span className="text-muted-foreground">{EMPTY}</span>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </AdminTableFrame>
            <TablePagination pagination={pg} />
          </>
        )}
      </>
    );
  }

  return (
    <div>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "감사 로그" }]}
        title="감사 로그"
        description={`최근 ${AUDIT_LIMIT}건의 관리 작업 기록입니다. 사용자·액션 필터는 받은 기록 안에서만 적용됩니다`}
      />
      {/* 「다시 시도」가 성공하면 포커스가 이 영역의 첫 컨트롤(사용자 필터)로 간다(ErrorState) */}
      <div data-testid="audit-log">{body}</div>
    </div>
  );
}
