import { execFile } from "node:child_process";
import path from "node:path";

// `pnpm --filter web exec`의 cwd는 apps/web이므로 저장소 루트를 명시 계산한다
// (note-provenance PLAN.md R3-04). apps/web/e2e/db.ts 기준 3단계 위가 저장소 루트.
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const COMPOSE_YML = path.join(REPO_ROOT, "infra", "compose.yml");
const COMPOSE_DEV_YML = path.join(REPO_ROOT, "infra", "compose.dev.yml");

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function runPsql(args: string[], sql: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(
      "docker",
      args,
      { cwd: REPO_ROOT },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(`${error.message}\n${stderr}`));
          return;
        }
        resolve(stdout);
      },
    );
    // psql -c는 변수 보간(:'var')을 지원하지 않는다(실측 확인 — 이 프로젝트 psql 16.15).
    // \set + stdin 스크립트 경로는 지원하므로 여기로 SQL을 흘려보낸다.
    child.stdin?.end(sql);
  });
}

export interface ReindexRunCounts {
  requested: number;
  queued: number;
  done: number;
  failed: number;
  /** 처리 전에 소스가 지워진 항목(코어 v0.12.0 — `queued`이면서 `source_id` NULL). */
  removed: number;
}

/**
 * 재인덱싱 run 1건의 등록 수와 항목 상태별 수를 DB에서 직접 읽는다(읽기 전용 — reindex-progress
 * PLAN D-15 · AC-17). `reindex_runs`에는 `requested`만 있고 진행 카운트는 `reindex_run_items`
 * 상태 집계다(biznotes-core `reindex_progress`와 같은 정의 — v0.12.0부터 `queued`는 소스가 남은 항목만,
 * 소스가 지워진 queued 항목은 `removed`). run_id는 UUID 형식 검증 후 psql
 * 변수(`\set rid` + `:'rid'`)로 바인딩하며, 정확히 1행이 아니면 throw한다.
 */
export async function fetchReindexRunCounts(runId: string): Promise<ReindexRunCounts> {
  if (!UUID_RE.test(runId)) {
    throw new Error(`fetchReindexRunCounts: runId가 UUID 형식이 아님: ${runId}`);
  }
  const user = process.env.POSTGRES_USER ?? "biznotes";
  const db = process.env.POSTGRES_DB ?? "biznotes";
  const sql = [
    `\\set rid ${runId}`,
    [
      "SELECT r.requested,",
      " count(i.id) FILTER (WHERE i.status = 'queued' AND i.source_id IS NOT NULL),",
      " count(i.id) FILTER (WHERE i.status = 'done'),",
      " count(i.id) FILTER (WHERE i.status = 'failed'),",
      " count(i.id) FILTER (WHERE i.status = 'queued' AND i.source_id IS NULL)",
      " FROM reindex_runs r LEFT JOIN reindex_run_items i ON i.run_id = r.id",
      " WHERE r.id = :'rid' GROUP BY r.id, r.requested;",
    ].join(""),
  ].join("\n");
  const stdout = await runPsql(
    [
      "compose",
      "-f",
      COMPOSE_YML,
      "-f",
      COMPOSE_DEV_YML,
      "exec",
      "-T",
      "postgres",
      "psql",
      "-v",
      "ON_ERROR_STOP=1",
      "-U",
      user,
      "-d",
      db,
      "-At",
      "-F",
      "|",
      "-q",
    ],
    sql,
  );
  const rows = stdout.split("\n").filter((line) => line.length > 0);
  if (rows.length !== 1) {
    throw new Error(
      `fetchReindexRunCounts: 정확히 1행이어야 함(runId=${runId}, rows=${JSON.stringify(rows)})`,
    );
  }
  const values = rows[0].split("|").map(Number);
  if (values.length !== 5 || values.some((value) => !Number.isInteger(value))) {
    throw new Error(`fetchReindexRunCounts: 응답 형식 오류(runId=${runId}, row=${rows[0]})`);
  }
  const [requested, queued, done, failed, removed] = values;
  return { requested, queued, done, failed, removed };
}

/**
 * 특정 assistant 메시지의 `provenance`를 NULL로 되돌려 legacy(2026-09 기능 도입 이전
 * 또는 기록 실패) 상태를 재현한다. e2e 전용 헬퍼 — `apps/web/e2e/`는 Next 빌드 대상 밖이라
 * 앱 코드 경로에 포함되지 않는다.
 *
 * 셸 보간 없이 execFile argv로 docker compose를 직접 호출한다. SQL 문자열에도 UUID를
 * 직접 넣지 않고 psql 변수(`\set` + `:'mid'`)로 바인딩한다 — messageId는 UUID 형식으로만
 * 검증해 `\set` 스크립트 라인에 개행 등 메타문자가 섞여 들어갈 여지를 차단한다.
 */
export async function setMessageProvenanceNull(messageId: string): Promise<void> {
  if (!UUID_RE.test(messageId)) {
    throw new Error(`setMessageProvenanceNull: messageId가 UUID 형식이 아님: ${messageId}`);
  }
  const user = process.env.POSTGRES_USER ?? "biznotes";
  const db = process.env.POSTGRES_DB ?? "biznotes";
  const sql = [
    `\\set mid ${messageId}`,
    "UPDATE chat_messages SET provenance = NULL WHERE id = :'mid' AND role = 'assistant' RETURNING id;",
  ].join("\n");

  const stdout = await runPsql(
    [
      "compose",
      "-f",
      COMPOSE_YML,
      "-f",
      COMPOSE_DEV_YML,
      "exec",
      "-T",
      "postgres",
      "psql",
      "-v",
      "ON_ERROR_STOP=1",
      "-U",
      user,
      "-d",
      db,
      "-At",
      // -q(quiet): 스크립트 경로에서는 -At만으로 명령 완료 태그("UPDATE 1")가 억제되지
      // 않는다(실측 확인) — RETURNING 행 뒤에 태그가 섞여 "정확히 1행" 단언이 깨진다.
      "-q",
    ],
    sql,
  );

  // false green 방지: 정확히 1행이 갱신되고 그 값이 messageId와 같아야 한다
  // (0행=대상 없음, 2행 이상=id 유일성 가정 위반 — 둘 다 legacy 재현 실패로 간주).
  const rows = stdout.split("\n").filter((line) => line.length > 0);
  if (rows.length !== 1 || rows[0] !== messageId) {
    throw new Error(
      `setMessageProvenanceNull: 예상과 다른 행이 갱신됨(messageId=${messageId}, rows=${JSON.stringify(rows)})`,
    );
  }
}
