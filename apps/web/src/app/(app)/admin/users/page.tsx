"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Users, UsersRound } from "lucide-react";
import { useCallback, useId, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { Toolbar, ToolbarSearch } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { type SecretRevealContent, SecretRevealDialog } from "@/components/ui/secret-reveal-dialog";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { StatusBadge } from "@/components/ui/status-badge";
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
import {
  ADMIN_USERS_KEY,
  RESET_BUSY_REASON,
  SELF_DEACTIVATE_REASON,
  useAdminUsers,
  usePasswordResetSecret,
  useSetUserActive,
} from "@/hooks/use-admin-users";
import { useMe } from "@/hooks/use-auth";
import { usePagination } from "@/hooks/use-pagination";
import { api, ApiError } from "@/lib/api-client";
import type { AdminGroup, AdminUser, GroupMember, UserLimits } from "@/lib/admin-types";
import { effectiveLimitText, USAGE_LIMIT_FIELDS, USAGE_LIMIT_SOURCE_LABEL } from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";
import { activeLabel, roleLabel } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import { FIRST_LOGIN_CHANGE_NOTICE, newPasswordErrorMessage } from "@/lib/password-policy";
import { qk } from "@/lib/query-keys";
import { useSecretScope } from "@/lib/secret-mutation";
import { DISPLAY_TIMEZONE_NAME } from "@/lib/timezones";

/**
 * 응답 행에 `must_change_password` 키가 있는가 — 강제 변경을 지원하는 API인지의 판정 근거다.
 * 생성 타입은 필수 필드라 런타임 검사(`in`)로 본다. 구 API 행에는 키가 없어 안내·배지를 숨긴다
 * (구 API에서는 사실이 아니다 — AC-3.4).
 */
function hasForcedChangeFlag(user: AdminUser): boolean {
  return "must_change_password" in user;
}

function CreateUserDialog({ forcedChangeSupported }: { forcedChangeSupported: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ email: "", name: "", password: "", role: "user" });
  const adminLabelId = useId();
  const create = useMutation({
    mutationFn: () => api.post<AdminUser>("/api/v1/admin/users", form),
    onSuccess: (created) => {
      const forced = hasForcedChangeFlag(created) && created.must_change_password === true;
      toast.success(
        "사용자를 추가했습니다",
        forced ? { description: FIRST_LOGIN_CHANGE_NOTICE } : undefined,
      );
      queryClient.invalidateQueries({ queryKey: ADMIN_USERS_KEY });
      setOpen(false);
      setForm({ email: "", name: "", password: "", role: "user" });
    },
    // 422는 reason 사전 문구로(코어 dict detail을 JSON 그대로 노출하지 않는다 — A-34, T-17)
    onError: (e) =>
      toast.error(e instanceof ApiError && e.status === 422 ? newPasswordErrorMessage(e) : e.message),
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button className="coarse:min-h-11" />}>
        <Plus aria-hidden />
        사용자 추가
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>사용자 추가</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="flex flex-col gap-3"
        >
          <Label htmlFor="u-email">이메일</Label>
          <Input
            id="u-email"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
          <Label htmlFor="u-name">이름</Label>
          <Input
            id="u-name"
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <Label htmlFor="u-pw">초기 비밀번호 (8자 이상)</Label>
          <Input
            id="u-pw"
            type="password"
            required
            minLength={8}
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          {forcedChangeSupported && (
            <p className="text-xs text-muted-foreground" data-testid="create-user-change-notice">
              {FIRST_LOGIN_CHANGE_NOTICE} — 새 사용자에게 이 비밀번호를 전달하면, 처음 로그인할 때
              본인이 새 비밀번호로 바꿉니다.
            </p>
          )}
          <div className="flex items-center gap-2 text-sm">
            <Switch
              aria-labelledby={adminLabelId}
              checked={form.role === "admin"}
              onCheckedChange={(c) => setForm({ ...form, role: c ? "admin" : "user" })}
            />
            <span id={adminLabelId}>관리자 권한</span>
          </div>
          <Button type="submit" disabled={create.isPending} className="coarse:min-h-11">
            추가
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function LimitsDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<Record<string, string>>({});
  const limits = useQuery({
    queryKey: qk.adminUserLimits.user(user?.id),
    queryFn: () => api.get<UserLimits>(`/api/v1/admin/users/${user!.id}/limits`),
    enabled: user !== null,
  });

  // 로드된 한도로 폼 초기화 (빈칸 = 사용자별 값 없음 — 일일 한도 기본값, 없으면 무제한)
  const loaded = limits.data;
  const formReady = user !== null && loaded !== undefined;
  const value = (key: string) =>
    form[key] ?? String(loaded?.[key as keyof UserLimits] ?? "");

  const save = useMutation({
    mutationFn: () =>
      api.put<UserLimits>(`/api/v1/admin/users/${user!.id}/limits`, {
        daily_tokens: value("daily_tokens") === "" ? null : Number(value("daily_tokens")),
        daily_agent_runs:
          value("daily_agent_runs") === "" ? null : Number(value("daily_agent_runs")),
      }),
    onSuccess: () => {
      notifySuccess("사용 한도를 저장했습니다");
      queryClient.invalidateQueries({ queryKey: qk.adminUserLimits.all });
      onClose();
      setForm({});
    },
    onError: (e) => notifyError("사용 한도를 저장", e),
  });

  return (
    <Dialog open={user !== null} onOpenChange={(o) => !o && (onClose(), setForm({}))}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>사용 한도 — {user?.email}</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          className="flex flex-col gap-3"
        >
          <p className="text-xs text-muted-foreground">
            빈칸 = 사용자 지정 없음 — 「사용 제한」의 일일 한도 기본값을 따르고, 기본값도 없으면 무제한입니다. 관리자
            역할은 한도 검사에서 제외됩니다. 사용량은 매일 {DISPLAY_TIMEZONE_NAME} 자정에 초기화됩니다.
          </p>
          {USAGE_LIMIT_FIELDS.map((f) => {
            // 적용 한도와 출처(코어 v0.26.0 #387 — 사용자 지정 > 기본값 > 무제한). 구 API 응답에는 없다
            const effective = loaded?.effective?.[f.key];
            return (
              <div key={f.key} className="flex flex-col gap-1">
                <div className="flex items-center gap-3">
                  <Label htmlFor={`limit-${f.key}`} className="w-36 shrink-0">
                    {f.label}
                  </Label>
                  <Input
                    id={`limit-${f.key}`}
                    type="number"
                    min={0}
                    placeholder={
                      effective?.source === "default" && effective.value !== null
                        ? `${USAGE_LIMIT_SOURCE_LABEL.default} ${formatNumber(effective.value)}`
                        : "무제한"
                    }
                    aria-describedby={effective ? `limit-${f.key}-effective` : undefined}
                    value={formReady ? value(f.key) : ""}
                    onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                  />
                  <span className="w-28 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
                    오늘 {formatNumber(loaded?.today?.[f.todayKey] ?? 0)}
                  </span>
                </div>
                {effective && (
                  <p
                    id={`limit-${f.key}-effective`}
                    data-testid={`limit-effective-${f.key}`}
                    className="pl-39 text-xs text-muted-foreground"
                  >
                    지금 적용: {effectiveLimitText(effective)}
                  </p>
                )}
              </div>
            );
          })}
          <Button type="submit" disabled={save.isPending || !formReady} className="coarse:min-h-11">
            저장
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CreateGroupDialog() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api.post("/api/v1/admin/groups", { name: name.trim() }),
    onSuccess: () => {
      notifySuccess("그룹을 추가했습니다");
      queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
      setName("");
      setOpen(false);
    },
    onError: (e) => notifyError("그룹을 추가", e),
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="sm" className="coarse:min-h-11" />}>
        <Plus aria-hidden />
        그룹 추가
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>그룹 추가</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) create.mutate();
          }}
          className="flex flex-col gap-3"
        >
          <Label htmlFor="g-name">그룹 이름</Label>
          <Input id="g-name" required value={name} onChange={(e) => setName(e.target.value)} />
          <Button
            type="submit"
            disabled={!name.trim() || create.isPending}
            className="coarse:min-h-11"
          >
            추가
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function GroupSection() {
  const queryClient = useQueryClient();
  const titleId = useId();
  const groups = useQuery({
    queryKey: ["admin-groups"],
    queryFn: () => api.get<AdminGroup[]>("/api/v1/admin/groups"),
  });
  const [memberTarget, setMemberTarget] = useState<AdminGroup | null>(null);
  const [memberEmailToAdd, setMemberEmailToAdd] = useState("");
  const pg = usePagination(groups.data ?? []);

  const users = useAdminUsers();
  const members = useQuery({
    queryKey: ["admin-group-members", memberTarget?.id],
    queryFn: () =>
      api.get<GroupMember[]>(`/api/v1/admin/groups/${memberTarget!.id}/members`),
    enabled: memberTarget !== null,
  });

  const deleteGroup = useMutation({
    mutationFn: (id: string) => api.delete(`/api/v1/admin/groups/${id}`),
    onSuccess: () => {
      notifySuccess("그룹을 삭제했습니다");
      queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
    },
    onError: (e) => notifyError("그룹을 삭제", e),
  });
  const addMember = useMutation({
    mutationFn: () => {
      const user = users.data?.find((u) => u.email === memberEmailToAdd);
      if (!user) throw new Error("해당 이메일의 사용자가 없습니다");
      return api.post(`/api/v1/admin/groups/${memberTarget!.id}/members`, {
        user_id: user.id,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-group-members"] });
      queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
      setMemberEmailToAdd("");
    },
    onError: (e) => notifyError("멤버를 추가", e),
  });
  const removeMember = useMutation({
    mutationFn: (userId: string) =>
      api.delete(`/api/v1/admin/groups/${memberTarget!.id}/members/${userId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-group-members"] });
      queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
    },
    onError: (e) => notifyError("멤버를 제거", e),
  });

  let body: React.ReactNode;
  if (groups.isPending) {
    body = <LoadingState variant="table" label="그룹을 불러오는 중" />;
  } else if (groups.isError) {
    body = (
      <ErrorState
        onRetry={() => void groups.refetch()}
        retrying={groups.isFetching}
      />
    );
  } else if (pg.total === 0) {
    body = (
      <EmptyState
        icon={<UsersRound />}
        title="아직 그룹이 없습니다"
        description="그룹을 만들면 노트북을 여러 사용자에게 한 번에 공유할 수 있습니다."
      />
    );
  } else {
    body = (
      <>
        <AdminTableFrame>
          <Table aria-labelledby={titleId}>
            <TableHeader>
              <TableRow>
                <TableHead>그룹 이름</TableHead>
                <TableHead className="text-right">멤버</TableHead>
                <TableHead className="w-40 text-right">동작</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pg.pageItems.map((g) => (
                <TableRow key={g.id}>
                  <TableCell>{g.name}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatNumber(g.member_count)}명
                  </TableCell>
                  <TableCell>
                    <RowActions
                      foldPrimaryOnNarrow
                      rowLabel={g.name}
                      primary={{ label: "멤버 관리", onClick: () => setMemberTarget(g) }}
                      items={[
                        {
                          label: "삭제…",
                          destructive: true,
                          confirm: {
                            title: `‘${g.name}’ 그룹을 삭제할까요?`,
                            description: "그룹의 멤버 구성과 그룹 공유가 함께 사라집니다. 되돌릴 수 없습니다.",
                            actionLabel: "삭제",
                          },
                          onConfirm: () => deleteGroup.mutate(g.id),
                        },
                      ]}
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
    <section aria-labelledby={titleId} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={titleId} tabIndex={-1} data-focus-fallback="" className="text-lg font-semibold">
          그룹
        </h2>
        <CreateGroupDialog />
      </div>
      {body}

      <Dialog open={memberTarget !== null} onOpenChange={(o) => !o && setMemberTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>멤버 관리 — {memberTarget?.name}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              addMember.mutate();
            }}
            className="flex gap-2"
          >
            <Input
              aria-label="추가할 사용자 이메일"
              placeholder="사용자 이메일"
              value={memberEmailToAdd}
              onChange={(e) => setMemberEmailToAdd(e.target.value)}
              list="user-emails"
            />
            <datalist id="user-emails">
              {(users.data ?? []).map((u) => (
                <option key={u.id} value={u.email} />
              ))}
            </datalist>
            <Button type="submit" size="sm" className="coarse:min-h-11">
              추가
            </Button>
          </form>
          <ul className="flex flex-col gap-1">
            {(members.data ?? []).map((m) => (
              <li key={m.user_id} className="flex items-center justify-between text-sm">
                <span>
                  {m.name} <span className="text-muted-foreground">({m.email})</span>
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="coarse:min-h-11"
                  aria-label={`${m.name} 제거`}
                  onClick={() => removeMember.mutate(m.user_id)}
                >
                  제거
                </Button>
              </li>
            ))}
          </ul>
        </DialogContent>
      </Dialog>
    </section>
  );
}

type RoleFilter = "all" | "admin" | "user";
const ROLE_FILTER_OPTIONS = [
  { value: "all", label: "전체" },
  { value: "admin", label: "관리자" },
  { value: "user", label: "사용자" },
] as const;

export default function AdminUsersPage() {
  const me = useMe();
  const users = useAdminUsers();
  const setActive = useSetUserActive();
  const [limitsTarget, setLimitsTarget] = useState<AdminUser | null>(null);
  const [query, setQuery] = useState("");
  const [role, setRole] = useState<RoleFilter>("all");
  const titleId = useId();

  // 비밀값 수명(D-22·D-28) — 이 화면의 임시 비밀번호는 scope의 React 상태 하나에만 있다
  const secrets = useSecretScope<SecretRevealContent>();
  const reset = usePasswordResetSecret(secrets);

  // 행의 `⋯` 트리거 — 결과 다이얼로그를 닫으면 요청한 행의 `⋯`로 포커스를 돌린다(AC-5.2 ③, 계약 ④)
  const triggers = useRef(new Map<string, HTMLButtonElement>());
  const triggerRefs = useRef(new Map<string, (el: HTMLButtonElement | null) => void>());
  const triggerRefFor = useCallback((id: string) => {
    let ref = triggerRefs.current.get(id);
    if (!ref) {
      ref = (el) => {
        if (el) triggers.current.set(id, el);
        else triggers.current.delete(id);
      };
      triggerRefs.current.set(id, ref);
    }
    return ref;
  }, []);
  const revealOrigin = useRef<string | null>(null);
  const returnToOrigin = useCallback(
    () => (revealOrigin.current ? triggers.current.get(revealOrigin.current) : null),
    [],
  );

  const all = useMemo(() => users.data ?? [], [users.data]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return all.filter(
      (u) =>
        (role === "all" || u.role === role) &&
        (q === "" || u.email.toLowerCase().includes(q) || u.name.toLowerCase().includes(q)),
    );
  }, [all, query, role]);
  const pg = usePagination(filtered);
  const forcedChangeSupported = all.some(hasForcedChangeFlag);

  function startReset(u: AdminUser) {
    revealOrigin.current = u.id;
    reset.start({ id: u.id, email: u.email, forcedChange: hasForcedChangeFlag(u) });
  }

  function changeActive(u: AdminUser, isActive: boolean) {
    setActive.mutate(
      { id: u.id, is_active: isActive },
      {
        onSuccess: () => notifySuccess(isActive ? "계정을 활성화했습니다" : "계정을 비활성화했습니다"),
        onError: (e) => notifyError(isActive ? "계정을 활성화" : "계정을 비활성화", e),
      },
    );
  }

  function rowItems(u: AdminUser): RowActionItem[] {
    const isSelf = me.data?.id === u.id;
    const items: RowActionItem[] = [
      {
        label: "비밀번호 초기화…",
        disabled: secrets.busy,
        disabledReason: secrets.busy ? RESET_BUSY_REASON : undefined,
        confirm: {
          title: `‘${u.email}’ 계정의 비밀번호를 초기화할까요?`,
          description:
            "지금 비밀번호로는 더 이상 로그인할 수 없습니다. 임시 비밀번호는 다음 창에서 한 번만 보입니다.",
          actionLabel: "초기화",
        },
        onSelect: () => startReset(u),
      },
    ];
    if (u.is_active) {
      items.push({
        label: "비활성화…",
        destructive: true,
        disabled: isSelf,
        disabledReason: isSelf ? SELF_DEACTIVATE_REASON : undefined,
        confirm: {
          title: `‘${u.email}’ 계정을 비활성화할까요?`,
          description: "로그인할 수 없게 됩니다. 나중에 다시 활성화할 수 있습니다.",
          actionLabel: "비활성화",
        },
        onConfirm: () => changeActive(u, false),
      });
    } else {
      items.push({ label: "활성화", onSelect: () => changeActive(u, true) });
    }
    return items;
  }

  let body: React.ReactNode;
  if (users.isPending) {
    body = <LoadingState variant="table" label="사용자를 불러오는 중" />;
  } else if (users.isError) {
    body = <ErrorState onRetry={() => void users.refetch()} retrying={users.isFetching} />;
  } else if (all.length === 0) {
    body = <EmptyState icon={<Users />} title="아직 사용자가 없습니다" />;
  } else if (filtered.length === 0) {
    body = (
      <FilteredEmptyState
        onClearFilters={() => {
          setQuery("");
          setRole("all");
        }}
      />
    );
  } else {
    body = (
      <>
        <AdminTableFrame>
          <Table aria-labelledby={titleId}>
            <TableHeader>
              <TableRow>
                <TableHead>이메일</TableHead>
                <TableHead>이름</TableHead>
                <TableHead>역할</TableHead>
                <TableHead>상태</TableHead>
                <TableHead className="w-44 text-right">동작</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pg.pageItems.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>{u.email}</TableCell>
                  <TableCell>{u.name}</TableCell>
                  <TableCell>
                    <StatusBadge tone={u.role === "admin" ? "info" : "neutral"}>
                      {roleLabel(u.role)}
                    </StatusBadge>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-1">
                      <StatusBadge tone={u.is_active ? "success" : "neutral"}>
                        {activeLabel(u.is_active)}
                      </StatusBadge>
                      {hasForcedChangeFlag(u) && u.must_change_password === true && (
                        <StatusBadge
                          tone="warning"
                          title={FIRST_LOGIN_CHANGE_NOTICE}
                          data-testid={`must-change-badge-${u.id}`}
                        >
                          변경 필요
                        </StatusBadge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <RowActions
                      foldPrimaryOnNarrow
                      rowLabel={u.email}
                      triggerRef={triggerRefFor(u.id)}
                      primary={{ label: "사용 한도", onClick: () => setLimitsTarget(u) }}
                      items={rowItems(u)}
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
    <div className="flex flex-col gap-8">
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "사용자·그룹" }]}
        title="사용자·그룹"
        description="계정과 역할, 그룹을 관리합니다"
        actions={<CreateUserDialog forcedChangeSupported={forcedChangeSupported} />}
      />

      <section aria-labelledby={titleId} className="flex flex-col gap-3">
        <h2 id={titleId} tabIndex={-1} data-focus-fallback="" className="text-lg font-semibold">
          사용자
        </h2>
        <Toolbar>
          <ToolbarSearch
            label="사용자 검색"
            placeholder="이메일 또는 이름으로 검색"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <SegmentedControl
            label="역할"
            options={ROLE_FILTER_OPTIONS}
            value={role}
            onValueChange={setRole}
          />
        </Toolbar>
        {body}
      </section>

      <GroupSection />

      <LimitsDialog user={limitsTarget} onClose={() => setLimitsTarget(null)} />
      <SecretRevealDialog
        content={secrets.revealed?.value ?? null}
        onClose={secrets.close}
        returnFocusTo={returnToOrigin}
      />
    </div>
  );
}
