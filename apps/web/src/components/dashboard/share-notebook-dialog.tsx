"use client";

import { useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useOpenSession } from "@/hooks/use-dialog-slot";
import { type MyGroup, useMyGroups } from "@/hooks/use-my-groups";
import { useNotebookShares, useRevokeNotebookShare, useShareWithGroup } from "@/hooks/use-notebook-shares";
import { useUpdateNotebook } from "@/hooks/use-notebooks";
import { ApiError } from "@/lib/api-client";
import {
  describeShareTarget,
  notebookPermissionLabel,
  SHARE_PERMISSION_OPTIONS,
  type ShareTargetKind,
} from "@/lib/labels";
import { BULK_MESSAGES, classifyError, type SharePermission } from "@/lib/notebook-bulk";
import { notifyError, notifySuccess } from "@/lib/notify";
import type { Notebook, NotebookShare } from "@/lib/types";

/** 대화상자가 쓰는 노트북 필드 — 전체 공개 토글은 지금 공개 범위와 실제 소유자 여부가 필요하다. */
export type ShareDialogNotebook = Pick<Notebook, "id" | "visibility" | "is_owner">;

/**
 * 노트북 공유 관리(core-v0.20.0 §5.8 ①~⑤ — 홈 카드·표의 소유자 메뉴와 노트북 화면 헤더가 함께 쓴다, 소유자만 연다).
 *
 * ① 그룹에 공유 — 소유자가 속한 그룹(`GET /me/groups`)과 권한을 골라 `POST /share {group_id, permission}`. 그룹이
 *    0개면 선택 대신 「공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요」(그룹은 관리자만 만든다).
 * ② 현재 공유(`GET /shares`) — 행마다 대상 표기(그룹 이름 / 「소유자가 속하지 않은 그룹 · id 앞 8자」)와 권한. 공유는
 *    그룹 대상뿐이다(코어 v0.28.0 #278 3단계). 이름을 주는 조회 계약은 늘리지 않는다(`group_id`만).
 * ③ 그룹 행 권한 변경(같은 `POST` — 같은 행 id가 돌아온다) ④ 행 해제(`DELETE /shares/{그 행 id}`, 확인 대화상자에
 *    대상 표기·권한) ⑤ 전체 공개 토글(`PATCH {visibility: "public" | "private"}` — 실제 소유자만).
 *
 * 관리 대화상자라 공유·변경·해제 뒤에도 열려 있다. 입력과 인라인 오류는 열린 회차마다 새로 시작한다(P3 재리뷰 P3-R01):
 * 닫고 다시 연(또는 다른 노트북으로 연) 뒤 이전 요청이 끝나면 토스트만 띄우고 새 창의 입력을 지우거나 그 창에 이전
 * 요청의 인라인 오류를 보이지 않는다. 알림은 요청마다 그 요청의 Promise(`mutateAsync`)로 붙인다(P3-R2-01).
 */
export function ShareNotebookDialog({
  notebook,
  open,
  onOpenChange,
}: {
  notebook: ShareDialogNotebook | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { session, isCurrent } = useOpenSession(open, notebook?.id ?? null);
  // 처음 포커스는 「그룹에 공유」 절 제목이다 — 그룹·공유 목록을 읽는 동안 첫 포커스 가능 요소는 「전체 공개」 스위치라,
  // 기본값이면 열자마자 누른 Space·Enter가 노트북을 공개로 바꿀 수 있다. `autoFocus`는 쓰지 않는다(이름 변경 다이얼로그와 같은 이유)
  const initialFocusRef = useRef<HTMLHeadingElement>(null);

  // 공유 행이 많아도 대화상자가 뷰포트를 넘지 않는다(PR #154 P3-03): 높이 상한은 뷰포트 − 위아래 1rem, 제목·닫기 버튼(오른쪽 위)은
  // 그대로 두고 본문만 세로로 스크롤한다. 공용 DialogContent(fixed·중앙)는 바꾸지 않고 이 대화상자의 className으로만 준다 —
  // grid 두 줄(제목 auto · 본문 minmax(0,1fr))이라 본문 칸이 남은 높이 안에서 줄고 그 안이 스크롤된다
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[calc(100vh-2rem)] grid-rows-[auto_minmax(0,1fr)] sm:max-w-lg supports-[height:100dvh]:max-h-[calc(100dvh-2rem)]"
        data-testid="share-dialog"
        initialFocus={initialFocusRef}
      >
        <DialogHeader>
          <DialogTitle>노트북 공유</DialogTitle>
          <DialogDescription>
            내가 속한 그룹에 읽기 전용·편집 가능 권한으로 공유하거나, 전체 공개로 모든 사용자가 읽게 합니다.
          </DialogDescription>
        </DialogHeader>
        {notebook && (
          <div className="-mx-4 min-h-0 overflow-y-auto overscroll-contain px-4 pb-1">
            <ShareManager
              key={session}
              notebook={notebook}
              isCurrentSession={() => isCurrent(session)}
              initialFocusRef={initialFocusRef}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** 해제 확인 대상 — 확인 대화상자가 열린 동안 그 행의 표기·권한을 붙잡아 둔다. */
interface RevokeTarget {
  share: NotebookShare;
  kind: ShareTargetKind;
  label: string;
  permissionLabel: string;
}

/** 한 회차의 공유 관리 — 이 인스턴스의 상태(입력·인라인 오류·대기)는 그 회차에만 산다(다시 열면 새 인스턴스). */
function ShareManager({
  notebook,
  isCurrentSession,
  initialFocusRef,
}: {
  notebook: ShareDialogNotebook;
  isCurrentSession: () => boolean;
  initialFocusRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const groups = useMyGroups(true);
  const shares = useNotebookShares(notebook.id);
  const revoke = useRevokeNotebookShare();
  const [revokeTarget, setRevokeTarget] = useState<RevokeTarget | null>(null);
  // 해제 확인이 닫힐 때 돌아갈 그 행의 「해제」 버튼. ConfirmDialog는 `returnFocusTo`의 정체가 바뀌면 열린 동안의 대기 상태를
  // 다시 초기화하므로 렌더마다 새로 만드는 함수가 아니라 같은 ref 객체를 넘긴다
  const revokeTriggerRef = useRef<HTMLElement | null>(null);
  const listHeadingRef = useRef<HTMLHeadingElement>(null);
  const formHeadingId = useId();
  const listHeadingId = useId();

  const groupNames = useMemo(
    () => (groups.data ? new Map(groups.data.map((g) => [g.id, g.name])) : null),
    [groups.data],
  );

  async function confirmRevoke() {
    const target = revokeTarget;
    if (!target) return;
    try {
      await revoke.mutateAsync({ notebookId: notebook.id, shareId: target.share.id });
      notifySuccess(`공유를 해제했습니다 — ${target.label} · ${target.permissionLabel}`);
    } catch (err) {
      notifyError("공유를 해제", err);
      // 이미 없는 행(404 "Share not found")이면 목록을 다시 읽어 사라지므로 확인 창을 닫는다. 그 밖의 실패는 열린 채 다시 시도
      if (err instanceof ApiError && err.status === 404) return;
      throw err;
    }
  }

  function renderList() {
    if (shares.isPending || groups.isPending) {
      return (
        <p role="status" className="text-sm text-muted-foreground" data-testid="share-list-loading">
          공유 목록을 불러오는 중…
        </p>
      );
    }
    if (shares.isError) {
      return (
        <div role="alert" className="flex items-center gap-2 text-sm" data-testid="share-list-error">
          <span className="text-destructive-text">공유 목록을 불러오지 못했습니다</span>
          <Button
            variant="outline"
            size="sm"
            className="coarse:min-h-11"
            onClick={() => void shares.refetch()}
            disabled={shares.isFetching}
          >
            다시 시도
          </Button>
        </div>
      );
    }
    return (
      <>
        <ul aria-labelledby={listHeadingId} className="flex flex-col gap-2" data-testid="share-list">
          {shares.data.map((share) => {
            const target = describeShareTarget(share, groupNames);
            return (
              <ShareRow
                key={share.id}
                notebookId={notebook.id}
                share={share}
                kind={target.kind}
                label={target.label}
                editable={target.editable}
                onRevoke={(trigger) => {
                  revokeTriggerRef.current = trigger;
                  setRevokeTarget({
                    share,
                    kind: target.kind,
                    label: target.label,
                    permissionLabel: notebookPermissionLabel(share.permission),
                  });
                }}
              />
            );
          })}
        </ul>
        {shares.data.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="share-list-empty">
            아직 공유한 대상이 없습니다
          </p>
        )}
      </>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <section aria-labelledby={formHeadingId} className="flex flex-col gap-3">
        <h3 id={formHeadingId} ref={initialFocusRef} tabIndex={-1} className="text-sm font-medium outline-none">
          그룹에 공유
        </h3>
        {groups.isPending ? (
          <p role="status" className="text-sm text-muted-foreground">
            그룹 목록을 불러오는 중…
          </p>
        ) : groups.isError ? (
          <div role="alert" className="flex items-center gap-2 text-sm" data-testid="share-groups-error">
            <span className="text-destructive-text">그룹 목록을 불러오지 못했습니다</span>
            <Button
              variant="outline"
              size="sm"
              className="coarse:min-h-11"
              onClick={() => void groups.refetch()}
              disabled={groups.isFetching}
            >
              다시 시도
            </Button>
          </div>
        ) : groups.data.length === 0 ? (
          <p className="rounded-lg bg-muted px-3 py-2 text-sm" data-testid="share-no-groups">
            {BULK_MESSAGES.noGroups}
          </p>
        ) : (
          <GroupShareForm notebookId={notebook.id} groups={groups.data} isCurrentSession={isCurrentSession} />
        )}
      </section>

      <section aria-labelledby={listHeadingId} className="flex flex-col gap-3">
        {/* 해제 확인 뒤 그 행이 사라지면 포커스가 이 제목으로 온다(확인 대화상자의 focusAfterConfirm) */}
        <h3 id={listHeadingId} ref={listHeadingRef} tabIndex={-1} className="text-sm font-medium outline-none">
          현재 공유
        </h3>
        {renderList()}
      </section>

      {notebook.is_owner && <PublicToggle notebook={notebook} />}

      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(next) => {
          if (!next) setRevokeTarget(null);
        }}
        title="공유를 해제할까요?"
        description={
          revokeTarget && (
            <>
              <span data-testid="share-revoke-target">
                {revokeTarget.label} · {revokeTarget.permissionLabel}
              </span>{" "}
              공유를 해제합니다.{" "}
              {revokeTarget.kind === "group"
                ? "이 그룹의 구성원은 이 공유로는 노트북을 더 볼 수 없습니다."
                : "해제한 뒤에는 이 화면에서 다시 공유할 수 없습니다."}
            </>
          )
        }
        actionLabel="해제"
        pendingLabel="해제하는 중…"
        awaitConfirm
        onConfirm={confirmRevoke}
        returnFocusTo={revokeTriggerRef}
        focusAfterConfirm={listHeadingRef}
      />
    </div>
  );
}

/** 새 공유 입력 — 그룹·권한. 성공하면 입력을 비우고(같은 회차일 때만) 대화상자는 열어 둔다. */
function GroupShareForm({
  notebookId,
  groups,
  isCurrentSession,
}: {
  notebookId: string;
  groups: readonly MyGroup[];
  isCurrentSession: () => boolean;
}) {
  const share = useShareWithGroup();
  const [groupId, setGroupId] = useState<string | null>(null);
  const [permission, setPermission] = useState<SharePermission>("read");
  const [pending, setPending] = useState(false);
  const [groupError, setGroupError] = useState<string | null>(null);
  const groupSelectId = useId();
  const groupErrorId = useId();
  const permissionId = useId();
  // Base UI Select는 닫힌 상태의 값(<SelectValue>)을 `items`로 라벨화한다 — 없으면 원시값(그룹 id)이 보인다
  const groupItems = useMemo(() => groups.map((g) => ({ value: g.id, label: g.name })), [groups]);
  const selected = groups.find((g) => g.id === groupId) ?? null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!selected || pending) return;
    const target = selected;
    const requested = permission;
    setGroupError(null);
    setPending(true);
    try {
      await share.mutateAsync({ notebookId, groupId: target.id, permission: requested });
      notifySuccess(`「${target.name}」 그룹에 ${notebookPermissionLabel(requested)} 권한으로 공유했습니다`);
      if (isCurrentSession()) {
        setGroupId(null);
        setPermission("read");
      }
    } catch (err) {
      // 그룹 404는 그 요청을 보낸 회차가 지금 회차일 때만 선택 칸 아래에 — 이미 닫은 창의 요청이면 토스트로 알린다
      if (classifyError(err).kind === "target_group_missing" && isCurrentSession()) {
        setGroupError(BULK_MESSAGES.targetGroupMissing);
      } else {
        notifyError("노트북을 공유", err);
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" aria-label="그룹에 공유">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label htmlFor={groupSelectId}>그룹</Label>
          <Select
            value={groupId}
            onValueChange={(v) => {
              const next = (v as string | null) ?? null;
              setGroupId(next);
              // 고른 그룹이 다시 읽은 목록에서 빠지면 Select가 값을 null로 바꾼다 — 그때는 그 그룹의 404 안내를 남긴다
              if (next !== null) setGroupError(null);
            }}
            items={groupItems}
            disabled={pending}
          >
            <SelectTrigger
              id={groupSelectId}
              className="w-full coarse:min-h-11"
              data-testid="share-group-select"
              aria-invalid={groupError ? true : undefined}
              aria-describedby={groupError ? groupErrorId : undefined}
            >
              <SelectValue placeholder="그룹 선택" />
            </SelectTrigger>
            <SelectContent>
              {groups.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {groupError && (
            <p id={groupErrorId} role="alert" className="text-xs text-destructive-text">
              {groupError}
            </p>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={permissionId}>권한</Label>
          <Select
            value={permission}
            onValueChange={(v) => v && setPermission(v as SharePermission)}
            items={SHARE_PERMISSION_OPTIONS}
            disabled={pending}
          >
            <SelectTrigger id={permissionId} className="w-full coarse:min-h-11" data-testid="share-permission-select">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SHARE_PERMISSION_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <p className="text-xs text-muted-foreground" data-testid="share-downgrade-hint">
        이미 공유된 그룹이면 권한이 이 값으로 바뀝니다(낮추기 포함)
      </p>
      <div className="flex justify-end">
        <Button type="submit" className="coarse:min-h-11" disabled={pending || !selected} data-testid="share-submit">
          {pending ? "공유하는 중…" : "공유"}
        </Button>
      </div>
    </form>
  );
}

/**
 * 현재 공유 한 행. 소속이 확인된 그룹 행(`editable`)만 권한을 바꿀 수 있고(같은 `POST` — 같은 행 id), 소유자가 속하지 않은 그룹
 * 행과 소속을 아직 모르는 그룹 행(`/me/groups` 조회 실패 — PR #154 P3-04)은 해제만 한다. 권한 변경 중에는 고른 값을
 * 보이고 그 행의 동작을 막으며, 실패하면 원래 값으로 돌아간다.
 */
function ShareRow({
  notebookId,
  share,
  kind,
  label,
  editable,
  onRevoke,
}: {
  notebookId: string;
  share: NotebookShare;
  kind: ShareTargetKind;
  label: string;
  editable: boolean;
  onRevoke: (trigger: HTMLElement) => void;
}) {
  const change = useShareWithGroup();
  const [pendingPermission, setPendingPermission] = useState<SharePermission | null>(null);
  const busy = pendingPermission !== null;
  const permissionLabel = notebookPermissionLabel(share.permission);

  async function changePermission(next: SharePermission) {
    if (!editable || busy || next === share.permission) return;
    setPendingPermission(next);
    try {
      await change.mutateAsync({ notebookId, groupId: share.group_id, permission: next });
      notifySuccess(`권한을 바꿨습니다 — ${label} · ${notebookPermissionLabel(next)}`);
    } catch (err) {
      notifyError("공유 권한을 변경", err);
    } finally {
      setPendingPermission(null);
    }
  }

  return (
    <li
      className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2"
      data-testid="share-row"
      data-share-id={share.id}
      data-share-kind={kind}
      data-permission={share.permission}
      aria-busy={busy || undefined}
    >
      <span className="min-w-0 flex-1 truncate text-sm" title={label} data-testid="share-row-target">
        {label}
      </span>
      {kind === "group" && editable ? (
        <Select
          value={pendingPermission ?? share.permission}
          onValueChange={(v) => v && void changePermission(v as SharePermission)}
          items={SHARE_PERMISSION_OPTIONS}
          disabled={busy}
        >
          <SelectTrigger className="w-32 coarse:min-h-11" aria-label={`${label} 권한`} data-testid="share-row-permission">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SHARE_PERMISSION_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <span className="text-sm text-muted-foreground" data-testid="share-row-permission-label">
          {permissionLabel}
        </span>
      )}
      <Button
        variant="outline"
        size="sm"
        className="coarse:min-h-11"
        aria-label={`${label} · ${permissionLabel} 공유 해제`}
        disabled={busy}
        onClick={(e) => onRevoke(e.currentTarget)}
        data-testid="share-row-revoke"
      >
        해제
      </Button>
    </li>
  );
}

/**
 * 전체 공개 토글(§5.8 ⑤ — 실제 소유자만 렌더). 누르면 곧바로 새 값을 보이고 `PATCH {visibility}`를 보낸다 — 실패하면
 * 원래 값으로 돌아간다. 응답이 오면 노트북 캐시(목록·상세)가 바뀌어 부모가 새 값을 넘긴다. 공개 범위는 `private`·`public`
 * 둘뿐이다(코어 v0.28.0 — 옛 `group` 값은 리비전이 `private`으로 바꿨다).
 */
function PublicToggle({ notebook }: { notebook: ShareDialogNotebook }) {
  const update = useUpdateNotebook();
  // 낙관 표시 — 그 값을 정할 때의 서버 값(`base`)이 그대로인 동안만 쓴다(부모가 새 값을 넘기면 그 값을 따른다)
  const [optimistic, setOptimistic] = useState<{ checked: boolean; base: Notebook["visibility"] } | null>(null);
  const [pending, setPending] = useState(false);
  const switchId = useId();
  const labelId = useId();
  const descId = useId();
  if (optimistic !== null && optimistic.base !== notebook.visibility) {
    // 렌더 중 파생 상태 정리(useOpenSession과 같은 방식) — 부모가 새 값을 넘겼으니 낙관 표시를 버린다
    setOptimistic(null);
  }
  const checked =
    optimistic !== null && optimistic.base === notebook.visibility ? optimistic.checked : notebook.visibility === "public";

  async function toggle(next: boolean) {
    if (pending) return;
    const base = notebook.visibility;
    setOptimistic({ checked: next, base });
    setPending(true);
    try {
      const updated = await update.mutateAsync({ id: notebook.id, visibility: next ? "public" : "private" });
      setOptimistic({ checked: updated.visibility === "public", base });
      notifySuccess(next ? "전체 공개를 켰습니다" : "전체 공개를 껐습니다");
    } catch (err) {
      setOptimistic(null);
      notifyError("전체 공개를 변경", err);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="flex items-start justify-between gap-3 rounded-lg border p-3" aria-labelledby={labelId}>
      <div className="flex min-w-0 flex-col gap-1">
        <Label id={labelId} htmlFor={switchId}>
          전체 공개
        </Label>
        <p id={descId} className="text-xs text-muted-foreground">
          켜면 모든 사용자가 이 노트북을 읽을 수 있습니다.
        </p>
      </div>
      <Switch
        id={switchId}
        aria-labelledby={labelId}
        aria-describedby={descId}
        checked={checked}
        onCheckedChange={(next) => void toggle(next)}
        disabled={pending}
        className="mt-1"
        data-testid="share-public-toggle"
      />
    </section>
  );
}
