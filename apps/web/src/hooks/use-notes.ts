"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api-client";
import type { Citation } from "@/lib/chat-types";
import type { Note, NoteOrigin } from "@/lib/note-types";
import type { Source } from "@/lib/source-types";

export interface NoteCreateBody {
  title: string;
  content: string;
  origin?: NoteOrigin;
  origin_ref?: string | null;
  // origin="agent"에서만 서버가 사용 — AgentRun.artifact_keys[].name. 다른 origin은 무시된다.
  artifact_name?: string | null;
  citations?: Citation[];
}

export function useNotes(notebookId: string) {
  return useQuery<Note[], ApiError>({
    queryKey: ["notes", notebookId],
    queryFn: () => api.get<Note[]>(`/api/v1/notebooks/${notebookId}/notes`),
  });
}

export function useCreateNote(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: NoteCreateBody) =>
      api.post<Note>(`/api/v1/notebooks/${notebookId}/notes`, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["notes", notebookId] }),
  });
}

export function useUpdateNote(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ noteId, ...body }: { noteId: string; title?: string; content?: string }) =>
      api.patch<Note>(`/api/v1/notes/${noteId}`, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["notes", notebookId] }),
  });
}

export function useDeleteNote(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) => api.delete<void>(`/api/v1/notes/${noteId}`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["notes", notebookId] }),
  });
}

export function useConvertNoteToSource(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) =>
      api.post<Source>(`/api/v1/notes/${noteId}/convert-to-source`, {}),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["sources", notebookId] }),
  });
}
