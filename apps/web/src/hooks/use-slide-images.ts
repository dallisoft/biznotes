"use client";

import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";
import { fetchArtifactObjectUrl } from "@/hooks/use-agents";
import type { AgentRun } from "@/lib/agent-types";
import { slideImageArtifacts } from "@/lib/slide-deck/parse";

/** 장 위치(0부터) → 삽화 object URL. 불러오지 못한 장은 빠진다(그 장은 이미지 없이 그린다). */
export type SlideImages = ReadonlyMap<number, string>;

const NO_IMAGES: SlideImages = new Map();

export interface SlideImagesState {
  images: SlideImages;
  /** 아직 받는 중인 삽화가 있다 — 인쇄·PPTX는 끝날 때까지 기다린다(그림이 빠진 파일 방지). */
  loading: boolean;
}

/**
 * 덱 run의 삽화 산출물(`slide-image-<n>.png`)을 인증 요청으로 받아 object URL로 돌려준다(core-v0.23.0-upgrade AC-10).
 * 성공 run의 산출물은 바뀌지 않으므로 다시 받지 않는다 — 오디오 산출물(`fetchArtifactObjectUrl`)과 같은 방식이다.
 * 실패는 재시도하지 않는다. object URL은 캐시가 항목을 버릴 때 해제한다(`installObjectUrlCleanup`).
 */
export function useSlideImages(run: AgentRun | undefined, total: number): SlideImagesState {
  const refs = useMemo(
    () => (run ? [...slideImageArtifacts(run.artifact_keys, total)] : []),
    [run, total],
  );
  const results = useQueries({
    queries: refs.map(([, ref]) => ({
      queryKey: ["artifact-image", run?.id, ref.name] as const,
      queryFn: () => fetchArtifactObjectUrl(run?.id as string, ref.name),
      staleTime: Infinity,
      retry: 0,
      refetchOnWindowFocus: false,
    })),
  });
  const urls = results.map((r) => r.data);
  const key = urls.join("\n");
  const loading = results.some((r) => r.isPending);
  const images = useMemo(() => {
    if (refs.length === 0) return NO_IMAGES;
    const out = new Map<number, string>();
    refs.forEach(([index], i) => {
      if (urls[i]) out.set(index, urls[i] as string);
    });
    return out;
    // 정당: `urls`는 렌더마다 새 배열이다 — 값이 같으면 같은 Map을 돌려주도록 이어 붙인 키로 비교한다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refs, key]);
  return { images, loading };
}
