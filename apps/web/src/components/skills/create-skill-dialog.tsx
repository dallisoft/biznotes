"use client";

import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useCreateSkill } from "@/hooks/use-skills";
import { notifyError, notifySuccess } from "@/lib/notify";

/** 서버 `SkillCreate` 제약(OpenAPI): name 1~120자, description 0~2000자. */
export const SKILL_NAME_MAX = 120;
export const SKILL_DESCRIPTION_MAX = 2000;

/**
 * 「새 스킬」 — 이름·설명을 받아 만든 뒤 편집 화면으로 간다(plan §6.6.2 스킬 목록, HCI-3 ②).
 * 즉시 「제목 없는 스킬」을 만들던 흐름을 대체한다. 이름이 비면 「만들기」는 비활성이다(AC-4.2).
 */
export function CreateSkillDialog() {
  const router = useRouter();
  const create = useCreateSkill();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const trimmed = name.trim();

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      setName("");
      setDescription("");
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!trimmed || create.isPending) return;
    create.mutate(
      { name: trimmed, description: description.trim() },
      {
        onSuccess: (skill) => {
          notifySuccess("스킬을 만들었습니다");
          onOpenChange(false);
          router.push(`/skills/${skill.id}/edit`);
        },
        onError: (err) => notifyError("스킬을 생성", err),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger render={<Button className="coarse:h-11" />}>
        <Plus aria-hidden />새 스킬
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>새 스킬</DialogTitle>
          <DialogDescription>이름과 설명을 정하면 편집 화면에서 SKILL.md를 작성합니다.</DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-skill-name">이름</Label>
            <Input
              id="new-skill-name"
              value={name}
              maxLength={SKILL_NAME_MAX}
              placeholder="예: 회의록 요약"
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-skill-description">설명 (선택)</Label>
            <Textarea
              id="new-skill-description"
              value={description}
              maxLength={SKILL_DESCRIPTION_MAX}
              rows={3}
              placeholder="목록에 보일 설명"
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={!trimmed || create.isPending} className="coarse:h-11">
              {create.isPending ? "만드는 중…" : "만들기"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
