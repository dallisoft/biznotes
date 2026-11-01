"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, Copy, FilePlus, Save, Trash2, Upload, XCircle } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { PageContainer } from "@/components/layout/page-container";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Textarea } from "@/components/ui/textarea";
import { retryFocusStatus, useRetryFocusLanding } from "@/hooks/use-retry-focus-landing";
import { ApiError } from "@/lib/api-client";
import { SKILL_STATUS_TONE, skillStatusLabel } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import {
  fetchSkillFile,
  useCloneSkill,
  useDeleteSkillFile,
  useFreezeSkill,
  usePublishSkillVersion,
  useSkillReviewRequests,
  useSkill,
  useSkillDraftFiles,
  useSkillFrontmatter,
  useUpdateSkill,
  useValidateSkill,
  useValidateSkillFrontmatter,
  useWriteSkillBinaryFile,
  useWriteSkillFile,
  useWriteSkillFrontmatter,
} from "@/hooks/use-skills";
import {
  REVIEW_KIND_LABEL,
  reviewPendingMessage,
  type SkillValidationResult,
} from "@/lib/skill-types";
import { SkillReportPanel } from "@/components/skills/skill-report-panel";

/** 폼으로 편집하는 문자열 필드. `metadata`·`allowed-tools`는 원문 그대로 보존한다. */
const FORM_FIELDS = [
  { field: "name", maxLength: 120, placeholder: "소문자-하이픈 (예: sales-summary)" },
  { field: "description", maxLength: 2000, placeholder: "무엇을 하고 언제 쓰는지" },
  { field: "license", maxLength: 200, placeholder: "선택 (예: Apache-2.0)" },
  { field: "compatibility", maxLength: 500, placeholder: "선택 (필요한 환경)" },
] as const;

type FormField = (typeof FORM_FIELDS)[number]["field"];

/**
 * 편집 권한이 없을 때(plan §6.5 ErrorState 권한 없음 변형, AC-4.5) — 막다른 화면 대신 「복제해서 내 스킬로 편집」과
 * 「스킬 상세로」를 준다. 판정(`can_edit`)은 바꾸지 않는다(§8.3).
 */
function SkillEditForbidden({ skillId }: { skillId: string }) {
  const router = useRouter();
  const clone = useCloneSkill();
  return (
    <PageContainer width="default">
      <ErrorState
        variant="forbidden"
        title="이 스킬을 편집할 권한이 없습니다"
        description="복제하면 내 스킬로 편집할 수 있습니다."
        actions={
          <Button
            className="coarse:h-11"
            disabled={clone.isPending}
            onClick={() =>
              clone.mutate(skillId, {
                onSuccess: (r) => {
                  notifySuccess("스킬을 복제했습니다");
                  router.push(`/skills/${r.skill_id}/edit`);
                },
                onError: (err) => notifyError("스킬을 복제", err),
              })
            }
          >
            <Copy aria-hidden />
            복제해서 내 스킬로 편집
          </Button>
        }
        back={{ href: `/skills/${skillId}`, label: "스킬 상세로" }}
      />
    </PageContainer>
  );
}

export default function SkillEditPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const skill = useSkill(id);
  const files = useSkillDraftFiles(id);
  const frontmatter = useSkillFrontmatter(id);
  const writeFile = useWriteSkillFile(id);
  const writeBinary = useWriteSkillBinaryFile(id);
  const writeFrontmatter = useWriteSkillFrontmatter(id);
  const deleteFile = useDeleteSkillFile(id);
  const validate = useValidateSkill(id);
  const validateFrontmatter = useValidateSkillFrontmatter(id);
  const { mutate: validateFrontmatterMutation } = validateFrontmatter;
  const freeze = useFreezeSkill(id);
  const publishVersion = usePublishSkillVersion(id);
  const updateMeta = useUpdateSkill(id);
  const reviews = useSkillReviewRequests(id);
  // 「다시 시도」는 오류 → 스켈레톤 → 결과로 화면 전체가 두 번 바뀐다 — 포커스가 body로 떨어지지 않게 로딩 동안은 셸 본문에,
  // 결과가 오면 본문 첫 포커스 가능 요소(브레드크럼·뒤로 링크)에 둔다(P2 회고 HCI-P2-2).
  const pageFocus = useRetryFocusLanding(retryFocusStatus(skill), "page");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<string>("SKILL.md");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [frontmatterFields, setFrontmatterFields] = useState<Record<string, unknown>>({});
  const [frontmatterDirty, setFrontmatterDirty] = useState(false);
  const [frontmatterSourceHash, setFrontmatterSourceHash] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [newPath, setNewPath] = useState("references/");
  const [validation, setValidation] = useState<SkillValidationResult | null>(null);

  const pendingReview = (reviews.data ?? []).find((r) => r.status === "pending");

  const selectedFile = (files.data ?? []).find((file) => file.path === selected);
  // 텍스트 여부는 서버가 정한다 — UTF-8이면 `text/plain`으로 내린다. 확장자 추정은
  // `.sh`(application/x-sh)·`.ts`(video/mp2t) 같은 스크립트를 바이너리로 오인한다.
  const fileContent = useQuery<string | null, ApiError>({
    queryKey: ["skill-file-content", id, selected],
    queryFn: () => fetchSkillFile(id, selected),
    enabled: !!selected,
  });
  const textFile = fileContent.data !== null;

  useEffect(() => {
    if (typeof fileContent.data === "string") {
      setContent(fileContent.data);
      setDirty(false);
    }
  }, [fileContent.data]);

  // 표시 이름·설명(목록 UI 전용)은 최초 로드 시 1회만 로컬 상태로 동기화 — 편집 중 refetch가 덮어쓰지 않도록 id 기준.
  // SKILL.md의 표준 name·description과는 별개다(biznotes-core 표준 계획 D2).
  useEffect(() => {
    if (skill.data) {
      setName(skill.data.name);
      setDescription(skill.data.description);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skill.data?.id]);

  function saveMeta(field: "name" | "description", value: string) {
    if (!skill.data || skill.data[field] === value) return;
    updateMeta.mutate(
      { [field]: value },
      { onError: (err) => toast.error(`저장 실패: ${err.message}`) },
    );
  }

  // 폼 서버 상태를 최초 로드 시 동기화한다. 주석/알 수 없는 키 문서는 fields가 비어 있고 원문 편집만 허용된다.
  useEffect(() => {
    if (frontmatter.data?.editable && !frontmatterDirty) {
      setFrontmatterFields(frontmatter.data.fields ?? {});
      setFrontmatterSourceHash(frontmatter.data.source_hash);
      setFrontmatterDirty(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frontmatter.data?.editable, frontmatter.data?.source_hash, skill.data?.id]);

  function updateFrontmatterField(field: FormField, value: string) {
    // 빈 선택 필드는 서버가 키를 지운다(빈 값이 flow 스타일로 직렬화되지 않게).
    setFrontmatterFields((current) => ({ ...current, [field]: value }));
    setFrontmatterDirty(true);
  }

  function formValue(field: FormField): string {
    const value = frontmatterFields[field];
    return typeof value === "string" ? value : "";
  }

  const formName = formValue("name");
  const formDescription = formValue("description");
  const formErrors = [
    ...(formName.trim() ? [] : ["name은 비어 있을 수 없습니다."]),
    ...(formDescription.trim() ? [] : ["description은 비어 있을 수 없습니다."]),
  ];

  function saveFrontmatter() {
    if (formErrors.length > 0) return;
    if (selected === "SKILL.md" && dirty) {
      toast.error("저장하지 않은 SKILL.md 원문을 먼저 저장하거나 되돌리세요.");
      return;
    }
    if (!frontmatterSourceHash) return;
    writeFrontmatter.mutate(
      { fields: frontmatterFields, sourceHash: frontmatterSourceHash },
      {
        onSuccess: (result) => {
          setFrontmatterFields(result.fields ?? {});
          setFrontmatterSourceHash(result.source_hash);
          setFrontmatterDirty(false);
          toast.success("frontmatter 저장됨");
        },
        onError: (err) => toast.error(`저장 실패: ${err.message}`),
      },
    );
  }

  useEffect(() => {
    if (
      !frontmatter.data?.editable ||
      !frontmatterDirty ||
      !frontmatterSourceHash ||
      formErrors.length > 0
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      validateFrontmatterMutation(
        { fields: frontmatterFields, sourceHash: frontmatterSourceHash },
        { onSuccess: (result) => setValidation(result) },
      );
    }, 350);
    return () => window.clearTimeout(timer);
  }, [
    formErrors.length,
    frontmatter.data?.editable,
    frontmatterDirty,
    frontmatterFields,
    frontmatterSourceHash,
    validateFrontmatterMutation,
  ]);

  function save() {
    writeFile.mutate(
      { path: selected, content },
      {
        onSuccess: () => {
          toast.success(`${selected} 저장됨`);
          setDirty(false);
          qc.invalidateQueries({ queryKey: ["skill-file-content", id, selected] });
        },
        onError: (err) => toast.error(`저장 실패: ${err.message}`),
      },
    );
  }

  function runValidate() {
    validate.mutate(undefined, {
      onSuccess: (r) => {
        setValidation(r);
        if (r.ok) toast.success("검증 통과");
        else toast.error(`검증 실패: ${r.message}`);
      },
    });
  }

  /** 동결 → (선택) 공개 관문. 공개는 별도 단계다(biznotes-core 표준 계획 D8 경로 1). */
  function doFreeze(asPublish: boolean) {
    freeze.mutate(
      {},
      {
        onSuccess: (r) => {
          if (!asPublish) {
            toast.success(`v${r.version} 동결됨`);
            return;
          }
          publishVersion.mutate(r.version_id, {
            onSuccess: (outcome) => {
              if (outcome.status === "review_pending") {
                toast.info("관리자 검토 대기 — 승인되면 조직에 공개됩니다");
              } else {
                toast.success(`v${r.version} 공개(org)했습니다`);
              }
            },
            onError: (err) => toast.error(`공개 실패: ${err.message}`),
          });
        },
        onError: (err) => toast.error(`실패: ${err.message}`),
      },
    );
  }

  function addFile() {
    if (!newPath.trim() || newPath.endsWith("/")) {
      toast.error("파일 경로를 입력하세요");
      return;
    }
    writeFile.mutate(
      { path: newPath, content: "" },
      {
        onSuccess: () => {
          toast.success(`${newPath} 추가됨`);
          setAddOpen(false);
          setSelected(newPath);
          setNewPath("references/");
        },
        onError: (err) => toast.error(`추가 실패: ${err.message}`),
      },
    );
  }

  function uploadBinary(file: File | undefined) {
    if (!file) return;
    const path = `assets/${file.name}`;
    if (
      (files.data ?? []).some((existing) => existing.path === path) &&
      !window.confirm(`${path} 파일이 이미 있습니다. 덮어쓸까요?`)
    ) {
      return;
    }
    writeBinary.mutate(
      { path, file },
      {
        onSuccess: () => {
          toast.success(`${path} 업로드됨`);
          setSelected(path);
        },
        onError: (err) => toast.error(`업로드 실패: ${err.message}`),
      },
    );
  }

  if (skill.isPending) {
    return (
      <PageContainer width="default">
        <Skeleton className="h-96" />
      </PageContainer>
    );
  }
  if (skill.isError || !skill.data) {
    const notFound =
      skill.error instanceof ApiError && (skill.error.status === 404 || skill.error.status === 403);
    return (
      <PageContainer width="default">
        <ErrorState
          title={notFound ? "스킬을 찾을 수 없습니다" : "스킬을 불러오지 못했습니다"}
          description={notFound ? "삭제되었거나 볼 수 없는 스킬입니다." : undefined}
          onRetry={
            notFound
              ? undefined
              : () => {
                  pageFocus.markRetry();
                  void skill.refetch();
                }
          }
          retrying={skill.isFetching}
          back={{ href: "/skills", label: "스킬 목록으로" }}
        />
      </PageContainer>
    );
  }
  if (!skill.data.can_edit) {
    return <SkillEditForbidden skillId={id} />;
  }

  const fileList = files.data ?? [];

  return (
    <PageContainer width="bleed" className="flex flex-col overflow-hidden">
      <header className="flex items-center justify-between gap-4 border-b px-6 py-3">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <Link
            href={`/skills/${id}`}
            aria-label="스킬 상세로"
            title="스킬 상세로"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 coarse:size-11"
          >
            <ArrowLeft aria-hidden className="size-4" />
          </Link>
          <Input
            value={name}
            maxLength={120}
            placeholder="스킬 이름"
            aria-label="표시 이름"
            onChange={(e) => setName(e.target.value)}
            onBlur={() => {
              const v = name.trim();
              if (!v) {
                setName(skill.data!.name);
                return;
              }
              setName(v);
              saveMeta("name", v);
            }}
            className="h-8 max-w-sm border-transparent bg-transparent px-1 text-lg font-semibold shadow-none hover:border-input focus-visible:ring-1"
          />
          <StatusBadge tone={SKILL_STATUS_TONE.draft}>{skillStatusLabel("draft")}</StatusBadge>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={runValidate} disabled={validate.isPending}>
            검증
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => doFreeze(false)}
            disabled={freeze.isPending}
          >
            동결
          </Button>
          <Button
            size="sm"
            onClick={() => doFreeze(true)}
            // freeze 진행 중 재클릭하면 version 2개 동결 + publish 2회가 된다(P0-18)
            disabled={freeze.isPending || publishVersion.isPending}
          >
            조직에 공유
          </Button>
        </div>
      </header>

      <div className="border-b px-6 py-2">
        <Input
          value={description}
          maxLength={2000}
          placeholder="목록에 보일 설명"
          aria-label="표시 설명"
          onChange={(e) => setDescription(e.target.value)}
          onBlur={() => saveMeta("description", description)}
          className="h-8 border-transparent bg-transparent px-1 text-sm text-muted-foreground shadow-none hover:border-input focus-visible:ring-1"
        />
      </div>

      <div className="border-b px-6 py-3">
        <p className="mb-2 text-xs font-semibold text-muted-foreground">
          SKILL.md frontmatter — 모델이 보는 이름·설명
        </p>
        {frontmatter.isPending ? (
          <Skeleton className="h-16" />
        ) : frontmatter.data?.editable ? (
          <div className="flex flex-col gap-2">
            <div className="grid gap-2 md:grid-cols-2">
              {FORM_FIELDS.map(({ field, maxLength, placeholder }) => (
                <div key={field} className="grid gap-1">
                  <Label htmlFor={`skill-frontmatter-${field}`}>{field}</Label>
                  <Input
                    id={`skill-frontmatter-${field}`}
                    value={formValue(field)}
                    maxLength={maxLength}
                    placeholder={placeholder}
                    onChange={(event) => updateFrontmatterField(field, event.target.value)}
                  />
                </div>
              ))}
            </div>
            {formErrors.length > 0 && (
              <p className="text-xs text-destructive">{formErrors.join(" ")}</p>
            )}
            <div>
              <Button
                size="sm"
                variant="outline"
                onClick={saveFrontmatter}
                disabled={!frontmatterDirty || formErrors.length > 0 || writeFrontmatter.isPending}
              >
                <Save className="size-4" /> frontmatter 저장
              </Button>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            이 frontmatter에는 {frontmatter.data?.reason === "frontmatter_comments" ? "주석이" : "알 수 없는 키 또는 파싱 오류가"} 있어
            폼 편집을 사용할 수 없습니다. SKILL.md 원문으로 편집하세요.
          </p>
        )}
      </div>

      {pendingReview && (
        <div className="border-b bg-warning/10 px-6 py-2 text-sm text-warning-text">
          관리자 검토 대기 중입니다 — {REVIEW_KIND_LABEL[pendingReview.kind] ?? pendingReview.kind}.{" "}
          {reviewPendingMessage(pendingReview.kind, skill.data?.origin)}
        </div>
      )}

      {validation && (
        <div className="flex flex-col gap-1 border-b px-6 py-2 text-sm">
          <div
            className={
              validation.ok
                ? "flex items-center gap-2 text-success-text"
                : "flex items-center gap-2 text-destructive"
            }
          >
            {validation.ok ? (
              <>
                <CheckCircle2 className="size-4" /> 검증 통과 · 파일 {validation.file_count}개
                {validation.references && validation.references.length > 0 && (
                  <span className="text-muted-foreground">
                    · 참조 {validation.references.length}개
                  </span>
                )}
              </>
            ) : (
              <>
                <XCircle className="size-4" /> [{validation.reason}] {validation.message}
              </>
            )}
          </div>
          <SkillReportPanel report={validation.report} />
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* 파일 매니저 */}
        <aside className="flex w-60 shrink-0 flex-col border-r">
          <div className="flex items-center justify-between px-3 py-2">
            <span className="text-xs font-semibold text-muted-foreground">파일</span>
            <div className="flex gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="size-6"
                aria-label="파일 추가"
                onClick={() => setAddOpen(true)}
              >
                <FilePlus className="size-4" />
              </Button>
              <label
                htmlFor="skill-binary-upload"
                aria-label="바이너리 업로드"
                className="inline-flex size-6 cursor-pointer items-center justify-center rounded-md hover:bg-accent"
              >
                <Upload className="size-4" />
              </label>
              <input
                id="skill-binary-upload"
                type="file"
                className="sr-only"
                onChange={(event) => {
                  uploadBinary(event.target.files?.[0]);
                  event.currentTarget.value = "";
                }}
              />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {files.isPending ? (
              <Skeleton className="h-32" />
            ) : (
              <ul className="flex flex-col gap-0.5">
                {fileList.map((f) => (
                  <li key={f.path}>
                    <button
                      type="button"
                      onClick={() => setSelected(f.path)}
                      className={`flex w-full items-center justify-between gap-1 rounded px-2 py-1 text-left text-xs ${
                        selected === f.path ? "bg-accent font-medium" : "hover:bg-accent/50"
                      }`}
                    >
                      <span className="truncate font-mono">{f.path}</span>
                      {f.path !== "SKILL.md" && (
                        <ConfirmButton
                          variant="ghost"
                          size="icon"
                          className="size-5 shrink-0"
                          title="파일 삭제"
                          description={`${f.path}를 삭제하시겠습니까?`}
                          onConfirm={() =>
                            deleteFile.mutate(f.path, {
                              onSuccess: () => {
                                toast.success("삭제됨");
                                if (selected === f.path) setSelected("SKILL.md");
                              },
                            })
                          }
                        >
                          <Trash2 className="size-3" />
                        </ConfirmButton>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        {/* 에디터 */}
        <section className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center justify-between border-b px-4 py-2">
            <span className="font-mono text-sm">{selected}</span>
            {textFile && (
            <Button
              size="sm"
              onClick={save}
              disabled={!dirty || writeFile.isPending || writeFrontmatter.isPending}
            >
                <Save className="size-4" /> 저장
              </Button>
            )}
          </div>
          {!textFile ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground">
              <Upload className="size-6" />
              <p>바이너리 파일은 편집기에서 열지 않습니다.</p>
              <p className="font-mono text-xs">{selectedFile?.size ?? 0} bytes</p>
              <p className="text-xs">동결 후 내보내면 업로드한 원본 바이트가 그대로 포함됩니다.</p>
            </div>
          ) : fileContent.isPending ? (
            <div className="p-4">
              <Skeleton className="h-full min-h-96" />
            </div>
          ) : (
            <Textarea
              value={content}
              spellCheck={false}
              disabled={writeFrontmatter.isPending}
              onChange={(e) => {
                setContent(e.target.value);
                setDirty(true);
              }}
              className="min-h-0 flex-1 resize-none overflow-auto rounded-none border-0 font-mono text-xs field-sizing-fixed focus-visible:ring-0"
            />
          )}
        </section>
      </div>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>파일 추가</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-path">경로 (상대)</Label>
            <Input
              id="new-path"
              value={newPath}
              placeholder="references/rules.md"
              onChange={(e) => setNewPath(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              references/, assets/, biznotes.json 등. 절대경로·.. 금지.
            </p>
          </div>
          <DialogFooter>
            <Button onClick={addFile} disabled={writeFile.isPending}>
              추가
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
