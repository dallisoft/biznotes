import { API_BASE_URL } from "@/lib/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    /** 파싱된 에러 본문 — `{reason, request_id}`처럼 detail 밖 필드를 읽을 때 쓴다. */
    public body?: unknown,
    /** 본문 `detail.code`(코어의 기계 판독용 오류 코드 — 예: `password_change_required`). 없으면 undefined. */
    public code?: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/** 코어 v0.11.0 #165 — 비밀번호를 바꿔야 하는 사용자가 허용 목록 밖 API를 부르면 받는 403 코드. */
export const PASSWORD_CHANGE_REQUIRED = "password_change_required";

type PasswordChangeListener = () => void;
const passwordChangeListeners = new Set<PasswordChangeListener>();

/**
 * 403 `password_change_required` 통지 구독. 해제 함수를 돌려준다.
 *
 * 판정은 모든 요청이 거치는 `throwIfNotOk` 한 곳에서만 한다 — 채팅 SSE·다운로드처럼 React
 * Query 밖에서 직접 부르고 예외를 소비하는 경로도 놓치지 않는다(PLAN D-33). 라우팅은 구독자
 * (`components/providers.tsx`)가 맡는다.
 */
export function onPasswordChangeRequired(listener: PasswordChangeListener): () => void {
  passwordChangeListeners.add(listener);
  return () => {
    passwordChangeListeners.delete(listener);
  };
}

function notifyPasswordChangeRequired(): void {
  for (const listener of [...passwordChangeListeners]) {
    try {
      listener();
    } catch {
      // 통지 실패가 원래 오류(ApiError) 전달을 막지 않는다
    }
  }
}

/** 오류 본문의 `detail.code` — `detail`이 객체이고 `code`가 문자열일 때만. */
function errorCode(body: unknown): string | undefined {
  if (typeof body !== "object" || body === null) return undefined;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) return undefined;
  const code = (detail as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

let refreshPromise: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  // 동시 401 발생 시 refresh 1회만 수행
  refreshPromise ??= fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
    method: "POST",
    credentials: "include",
  })
    .then((r) => r.ok)
    .catch(() => false)
    .finally(() => {
      setTimeout(() => {
        refreshPromise = null;
      }, 0);
    });
  return refreshPromise;
}

/**
 * 401 시 토큰을 1회 갱신하고 재시도한 뒤 `Response`를 그대로 돌려준다.
 *
 * 본문을 소비하지 않으므로 JSON·텍스트·blob·SSE 어디서든 재사용할 수 있다.
 * 재시도는 **응답 헤더 수신 시점**에만 일어난다 — 스트림을 읽기 시작한 뒤에는
 * 부분 소비된 본문을 재생할 수 없으므로 재시도하지 않는다(HCI-R4-4).
 */
async function fetchWithRefresh(
  path: string,
  init: RequestInit = {},
  retried = false,
): Promise<Response> {
  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, credentials: "include" });

  if (res.status === 401 && !retried && !path.startsWith("/api/v1/auth/")) {
    const refreshed = await tryRefresh();
    if (refreshed) return fetchWithRefresh(path, init, true);
  }
  return res;
}

/**
 * 오류 본문의 `detail`을 사람이 읽을 한 줄로 만든다(#96). 원본은 `ApiError.body`에 남는다.
 *
 * - 문자열이면 그대로.
 * - 객체(코어 `{code, detail, reason}`)면 `detail` 문자열, 없으면 `message` 문자열.
 * - 목록(FastAPI 검증 422)이면 항목의 `msg`만 모은다 — `input`(비밀번호일 수 있다)은 버린다(#93).
 * - 그 밖(키 없음·빈 값)은 `fallback`(statusText).
 */
function detailText(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (typeof item === "object" && item !== null ? (item as { msg?: unknown }).msg : undefined))
      .filter((msg): msg is string => typeof msg === "string" && msg.trim() !== "");
    return messages.length > 0 ? messages.join("; ") : fallback;
  }
  if (typeof detail === "object" && detail !== null) {
    for (const key of ["detail", "message"] as const) {
      const text = (detail as Record<string, unknown>)[key];
      if (typeof text === "string" && text.trim() !== "") return text;
    }
  }
  return fallback;
}

/**
 * 에러 응답을 `ApiError`로 변환한다(본문이 JSON이 아니어도 안전).
 *
 * `res.ok`면 본문을 읽지 않는다 — 정상 SSE 스트림은 판정 대상이 아니다. 403
 * `password_change_required`면 던지기 전에 구독자에게 알린다(AC-3.1).
 */
async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  let detail = res.statusText;
  let parsed: unknown;
  try {
    const body = await res.json();
    parsed = body;
    detail = detailText(body, res.statusText);
  } catch {
    // 바디 없는 에러 응답
  }
  const code = errorCode(parsed);
  if (res.status === 403 && code === PASSWORD_CHANGE_REQUIRED) notifyPasswordChangeRequired();
  throw new ApiError(res.status, detail, parsed, code);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetchWithRefresh(path, {
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  await throwIfNotOk(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  /** `init.signal`로 요청을 취소할 수 있다 — 취소되면 fetch의 `AbortError`가 그대로 전달된다. */
  get: <T>(path: string, init?: { signal?: AbortSignal }) =>
    request<T>(path, init?.signal ? { signal: init.signal } : {}),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  /**
   * 파일 원본을 변환 없이 PUT한다. JSON 쓰기와 달리 Content-Type을 호출자가 명시한다.
   * Skill asset처럼 해시와 export 바이트가 중요한 데이터에 쓴다.
   */
  putBytes: async <T>(path: string, body: BodyInit, contentType = "application/octet-stream") => {
    const res = await fetchWithRefresh(path, {
      method: "PUT",
      body,
      headers: { "Content-Type": contentType },
    });
    await throwIfNotOk(res);
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  },
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),

  /**
   * `FormData` 업로드. **`Content-Type`을 절대 설정하지 않는다** —
   * 브라우저가 `multipart/form-data; boundary=…`를 직접 붙여야 하며,
   * 수동으로 지정하면 boundary가 없어 서버가 파싱하지 못한다(HCI-R4-4).
   */
  postForm: async <T>(path: string, form: FormData, signal?: AbortSignal): Promise<T> => {
    const res = await fetchWithRefresh(path, { method: "POST", body: form, signal });
    await throwIfNotOk(res);
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  },

  /** 텍스트 본문(스킬 파일 원문 등). */
  getText: async (path: string): Promise<string> => {
    const res = await fetchWithRefresh(path);
    await throwIfNotOk(res);
    return res.text();
  },

  /** 바이너리 본문(내보내기·아티팩트 다운로드 등). */
  getBlob: async (path: string): Promise<Blob> => {
    const res = await fetchWithRefresh(path);
    await throwIfNotOk(res);
    return res.blob();
  },

  /**
   * SSE 등 스트리밍 응답. 본문을 소비하지 않은 `Response`를 그대로 넘긴다.
   * 401 재시도는 헤더 수신 시점까지만 유효하다(위 `fetchWithRefresh` 주석).
   */
  stream: async (path: string, init: RequestInit = {}): Promise<Response> => {
    const res = await fetchWithRefresh(path, {
      ...init,
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    await throwIfNotOk(res);
    return res;
  },
};
