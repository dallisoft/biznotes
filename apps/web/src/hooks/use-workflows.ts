"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api-client";
import type { CallableAgent, Workflow, WorkflowDefinition } from "@/lib/workflow-types";
import type { ParamsSchema } from "@/lib/agent-types";

export function useWorkflows() {
  return useQuery<Workflow[], ApiError>({
    queryKey: ["workflows"],
    queryFn: () => api.get<Workflow[]>("/api/v1/workflows"),
    staleTime: 15_000,
  });
}

export function useWorkflow(id: string | null) {
  return useQuery<Workflow, ApiError>({
    queryKey: ["workflow", id],
    queryFn: () => api.get<Workflow>(`/api/v1/workflows/${id}`),
    enabled: !!id,
  });
}

export function useCallableAgents() {
  return useQuery<CallableAgent[], ApiError>({
    queryKey: ["workflows-callable"],
    queryFn: () => api.get<CallableAgent[]>("/api/v1/workflows/callable"),
    staleTime: 30_000,
  });
}

export function useCreateWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      api.post<Workflow>("/api/v1/workflows", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

export function useUpdateWorkflow(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name?: string;
      description?: string;
      icon?: string;
      definition?: WorkflowDefinition;
      params_schema?: ParamsSchema;
    }) => api.patch<Workflow>(`/api/v1/workflows/${id}`, body),
    onSuccess: (data) => {
      qc.setQueryData(["workflow", id], data);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
}

export function useDeleteWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/api/v1/workflows/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

export function useDuplicateWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Workflow>(`/api/v1/workflows/${id}/duplicate`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflows"] }),
  });
}

export function usePublishWorkflow(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (publish: boolean) =>
      api.post<Workflow>(`/api/v1/workflows/${id}/${publish ? "publish" : "unpublish"}`),
    onSuccess: (data) => {
      qc.setQueryData(["workflow", id], data);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
}
