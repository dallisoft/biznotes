# 보안 정책 / Security policy

## 한국어

### 신고 방법

취약점은 공개 이슈에 쓰지 말고 GitHub의 비공개 신고로 알려 주세요: 저장소 **Security › Report a vulnerability** ([바로가기](https://github.com/dalli/biznotes/security/advisories/new)).

다음을 적어 주시면 빨리 확인할 수 있습니다.

- 영향받는 버전·커밋과 설정(`APP_ENV`, `STORAGE_BACKEND`, 샌드박스 사용 여부 등)
- 재현 절차와 기대한 동작·실제 동작
- 영향(어떤 경계를 넘는지)과 알고 있는 우회책

관리자가 소규모라 응답 시간을 보장하지는 못하지만, 받은 신고는 확인하는 대로 답하고 수정·공개 일정을 함께 정합니다. 수정이 릴리스되기 전까지는 내용을 공개하지 말아 주세요.

### 지원 버전

가장 최근 릴리스와 `main`만 보안 수정을 받습니다.

### 특히 관심 있는 영역

- 인증·권한: 다른 사용자의 노트북·소스·실행에 접근, 관리 API 우회, JWT 위조
- 스킬 스크립트 샌드박스 탈출: Landlock·seccomp 우회, 서명 URL 토큰 위조·재사용, 데이터 볼륨 접근
- SSRF 관문 우회: 내부 주소·메타데이터 엔드포인트 도달, DNS 재바인딩
- 저장된 비밀 값(LLM API 키 등) 노출, 로그로 새는 토큰
- 업로드·인제스트 경로의 경로 탈출, 압축 폭탄

### 범위 밖

- `APP_ENV`가 `prod`가 아닐 때 저장소에 공개된 개발용 기본 비밀값과 시드 계정 비밀번호(운영 전에 바꾸도록 README에 적혀 있습니다)
- 관리자가 의도적으로 허용한 설정의 결과(예: 에이전트 외부 연결을 넓게 연 경우, 신뢰하지 않는 MCP 서버 등록)
- 연결한 LLM 프로바이더·SearXNG 등 외부 서비스 자체의 문제

## English

### Reporting

Please do not open public issues for vulnerabilities. Report them privately through GitHub: **Security › Report a vulnerability** on this repository ([direct link](https://github.com/dalli/biznotes/security/advisories/new)). Include the affected version or commit and configuration, reproduction steps, and the impact. This is a small project and cannot promise response times, but every report will be answered once reviewed, and a fix and disclosure timeline agreed with you. Please keep details private until a fix is released.

### Supported versions

Only the latest release and `main` receive security fixes.

### Areas of particular interest

Authentication and authorization bypass, escapes from the skill script sandbox (Landlock/seccomp bypass, forging or replaying signed storage URLs, reaching the data volume), SSRF gate bypass, exposure of stored secrets, and path traversal or decompression bombs in uploads and ingestion.

### Out of scope

Public development defaults (secrets and seed passwords) when `APP_ENV` is not `prod`, consequences of settings an administrator deliberately enabled, and issues in connected third-party services (LLM providers, SearXNG).
