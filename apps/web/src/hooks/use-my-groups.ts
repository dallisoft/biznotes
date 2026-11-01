"use client";

import { useQuery } from "@tanstack/react-query";
import type { components } from "@biznotes/shared-types";
import { api, type ApiError } from "@/lib/api-client";
import { qk } from "@/lib/query-keys";

/**
 * 요청자가 속한 그룹 한 개(`GET /api/v1/me/groups`의 원소). 생성 타입 별칭이다 — 수동 미러를 두지 않는다
 * (core-v0.20.0 P3 발주: 새 별칭은 훅 파일 안에 둔다).
 */
export type MyGroup = components["schemas"]["MyGroupOut"];

/**
 * 공유 대상으로 고를 수 있는 그룹 — 요청자(노트북 소유자)가 속한 그룹만이다(코어 `GET /me/groups`, admin도 자기 소속만).
 * 코어는 노트북 실제 소유자가 속하지 않은 그룹에 공유하면 `404 "Target group not found"`로 거절한다.
 * 다이얼로그가 열려 있을 때만 읽는다(`enabled`).
 */
export function useMyGroups(enabled = true) {
  return useQuery<MyGroup[], ApiError>({
    queryKey: qk.myGroups.all,
    queryFn: () => api.get<MyGroup[]>("/api/v1/me/groups"),
    enabled,
  });
}
