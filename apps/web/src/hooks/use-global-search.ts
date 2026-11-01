"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api-client";

export interface ChunkHit {
  chunk_id: string;
  notebook_id: string;
  source_id: string;
  source_title: string;
  snippet: string;
  score: number;
}

export interface SearchResults {
  notebooks: { id: string; title: string; emoji: string }[];
  sources: { id: string; notebook_id: string; title: string; type: string }[];
  notes: { id: string; notebook_id: string; title: string; snippet: string }[];
  chunks?: ChunkHit[];
}

// 300ms 디바운스된 전체 검색 (/api/v1/search)
export function useGlobalSearch(input: string) {
  const [query, setQuery] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setQuery(input.trim()), 300);
    return () => clearTimeout(t);
  }, [input]);

  const results = useQuery<SearchResults>({
    queryKey: ["global-search", query],
    queryFn: () => api.get<SearchResults>(`/api/v1/search?q=${encodeURIComponent(query)}`),
    enabled: query.length >= 1,
  });

  return { query, results };
}

// 시맨틱(본문) 검색 — 임베딩 비용이 있어 사용자가 명시적으로 트리거할 때만 실행
export function useSemanticSearch(submittedQuery: string) {
  return useQuery<SearchResults>({
    queryKey: ["semantic-search", submittedQuery],
    queryFn: () =>
      api.get<SearchResults>(
        `/api/v1/search?q=${encodeURIComponent(submittedQuery)}&mode=semantic`,
      ),
    enabled: submittedQuery.length >= 2,
    staleTime: 30_000,
  });
}
