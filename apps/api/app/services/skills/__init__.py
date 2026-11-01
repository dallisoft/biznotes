"""파일 기반 Agent Skill 패키지 서비스 (skill_plan.md §9).

package    — 경로 검증, 패키지 해시, 파일 트리 빌드, 스토리지 저장(drafts/versions)
parser     — SKILL.md frontmatter 파싱 + body 분리
validator  — 파일 제한 / manifest schema / 보안 스캔
builder    — SkillVersion 로드 → 런타임 노드 + 스냅샷 구성
"""
