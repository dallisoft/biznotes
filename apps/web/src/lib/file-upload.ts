export const MAX_FILE_UPLOAD_CONCURRENCY = 4;

export const UPLOAD_RESULT_KEYS = [
  "existingClientSkipped",
  "sameBatchSkipped",
  "oversizedClientSkipped",
  "serverDuplicate409",
  "uploaded",
  "validationOrCapacity422",
  "tooLarge413",
  "rateLimited429",
  "networkFailed",
] as const;

export type UploadResultKey = (typeof UPLOAD_RESULT_KEYS)[number];

export type UploadResultCounts = Record<UploadResultKey, number>;

export type ExistingFileSource = {
  type: string;
  title: string;
  size_bytes: number | null;
};

export type NewFileSelection = {
  files: File[];
  counts: UploadResultCounts;
};

export type FileUploadBatchResult<T> = {
  counts: UploadResultCounts;
  uploaded: T[];
};

export type UploadFilesOptions = {
  initialCounts?: Partial<UploadResultCounts>;
  maxConcurrency?: number;
  signal?: AbortSignal;
};

export function emptyUploadResultCounts(): UploadResultCounts {
  return {
    existingClientSkipped: 0,
    sameBatchSkipped: 0,
    oversizedClientSkipped: 0,
    serverDuplicate409: 0,
    uploaded: 0,
    validationOrCapacity422: 0,
    tooLarge413: 0,
    rateLimited429: 0,
    networkFailed: 0,
  };
}

/**
 * Returns the same filename identity used by the upload duplicate policy:
 * basename only, Unicode NFC, trimmed whitespace, and case-sensitive.
 */
export function canonicalizeFilename(filename: string): string {
  const lastSeparator = Math.max(filename.lastIndexOf("/"), filename.lastIndexOf("\\"));
  return filename.slice(lastSeparator + 1).normalize("NFC").trim();
}

/** File.size is defined in bytes, so no character-length conversion is needed. */
export function canonicalFileIdentity(file: Pick<File, "name" | "size">): string {
  return `${canonicalizeFilename(file.name)}\u0000${file.size}`;
}

function canonicalSourceIdentity(source: ExistingFileSource): string | null {
  if (source.type !== "file" || source.size_bytes === null) return null;
  return `${canonicalizeFilename(source.title)}\u0000${source.size_bytes}`;
}

/**
 * Filters client-known duplicates while retaining input order. Server-side
 * duplicate checks remain authoritative for races and stale React Query data.
 */
export function filterNewFiles(
  files: readonly File[],
  existingSources: readonly ExistingFileSource[],
): NewFileSelection {
  const counts = emptyUploadResultCounts();
  const existingKeys = new Set(
    existingSources
      .map(canonicalSourceIdentity)
      .filter((key): key is string => key !== null),
  );
  const batchKeys = new Set<string>();
  const newFiles: File[] = [];

  for (const file of files) {
    const key = canonicalFileIdentity(file);
    if (existingKeys.has(key)) {
      counts.existingClientSkipped += 1;
    } else if (batchKeys.has(key)) {
      counts.sameBatchSkipped += 1;
    } else {
      batchKeys.add(key);
      newFiles.push(file);
    }
  }

  return { files: newFiles, counts };
}

export type SizeCheckedSelection = {
  files: File[];
  /** 업로드 최대 크기를 넘어 보내지 않는 파일(입력 순서) */
  oversized: File[];
};

/**
 * 업로드 최대 크기(`GET /sources/upload-limits`, 코어 v0.26.0 #388)를 넘는 파일을 보내기 전에 걸러낸다. 서버도 같은
 * 값으로 판정하고 넘으면 413이다 — 이 검사는 큰 파일을 헛되이 보내지 않게 하는 사전 단계이고, 413이 최종 관문이다.
 */
export function splitOversizedFiles(files: readonly File[], maxBytes: number): SizeCheckedSelection {
  const accepted: File[] = [];
  const oversized: File[] = [];
  for (const file of files) (file.size > maxBytes ? oversized : accepted).push(file);
  return { files: accepted, oversized };
}

function errorStatus(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null || !("status" in error)) return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : undefined;
}

function errorResultKey(
  error: unknown,
): Exclude<UploadResultKey, "uploaded" | "existingClientSkipped" | "sameBatchSkipped" | "oversizedClientSkipped"> {
  switch (errorStatus(error)) {
    case 409:
      return "serverDuplicate409";
    case 413:
      // 코어 업로드 관문(`X-Admission-Code: too_large`)이나 프록시 본문 상한 — 사전 검사를 지나도 받는 최종 판정
      return "tooLarge413";
    case 422:
      return "validationOrCapacity422";
    case 429:
      return "rateLimited429";
    default:
      return "networkFailed";
  }
}

/**
 * Uploads without retrying and with at most four in-flight requests. Results
 * are reduced in input order, so completion timing cannot change accounting.
 */
export async function uploadFilesWithConcurrency<T>(
  files: readonly File[],
  upload: (file: File, signal?: AbortSignal) => Promise<T>,
  options: UploadFilesOptions = {},
): Promise<FileUploadBatchResult<T>> {
  const counts = { ...emptyUploadResultCounts(), ...options.initialCounts };
  const outcomes: Array<{ ok: true; value: T } | { ok: false; key: UploadResultKey }> = [];
  let nextIndex = 0;
  const maxConcurrency = Math.max(
    1,
    Math.min(MAX_FILE_UPLOAD_CONCURRENCY, Math.floor(options.maxConcurrency ?? MAX_FILE_UPLOAD_CONCURRENCY)),
  );

  async function worker() {
    while (true) {
      if (options.signal?.aborted) return;
      const index = nextIndex++;
      if (index >= files.length) return;
      try {
        outcomes[index] = { ok: true, value: await upload(files[index], options.signal) };
      } catch (error) {
        if (options.signal?.aborted) return;
        outcomes[index] = { ok: false, key: errorResultKey(error) };
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(maxConcurrency, files.length) }, worker));

  const uploaded: T[] = [];
  for (const outcome of outcomes) {
    if (!outcome) continue;
    if (outcome.ok) {
      counts.uploaded += 1;
      uploaded.push(outcome.value);
    } else {
      counts[outcome.key] += 1;
    }
  }

  return { counts, uploaded };
}
