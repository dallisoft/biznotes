"use client";

import { useQuery } from "@tanstack/react-query";
import type { components } from "@biznotes/shared-types";
import { api, type ApiError } from "@/lib/api-client";
import { qk } from "@/lib/query-keys";

/** 앱·코어 버전(`GET /api/v1/meta/versions`). 생성 타입 별칭이다 — 수동 미러를 두지 않는다. */
export type Versions = components["schemas"]["VersionsOut"];

/** 사이드바 브랜드 행 아래 표기용. 값은 배포 때만 바뀌므로 세션 동안 한 번만 읽는다. */
export function useVersions() {
  return useQuery<Versions, ApiError>({
    queryKey: qk.versions.all,
    queryFn: () => api.get<Versions>("/api/v1/meta/versions"),
    staleTime: Infinity,
  });
}
