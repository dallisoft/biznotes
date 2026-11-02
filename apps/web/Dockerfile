# 빌드 컨텍스트 = 모노레포 루트 (pnpm workspace lockfile 사용)
# docker build -f apps/web/Dockerfile .
FROM node:26-alpine AS builder

RUN corepack enable pnpm
WORKDIR /repo

COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/shared-types/package.json packages/shared-types/
RUN pnpm install --frozen-lockfile --filter web --filter @biznotes/shared-types

COPY packages/shared-types packages/shared-types
COPY apps/web apps/web

ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:28000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN pnpm --filter web build

FROM node:26-alpine

# tzdata: Node는 ICU로 TZ(compose·Helm이 주는 Asia/Seoul)를 이미 따르지만, alpine에는 zoneinfo가
# 없어 셸·헬스체크 같은 libc 경로는 UTC로 남는다(docs/plans/container-kst-timezone.md D-5).
RUN apk add --no-cache tzdata \
    && addgroup -S nodejs -g 1001 && adduser -S nextjs -u 1001
WORKDIR /app

# 모노레포 standalone 출력은 리포 구조를 보존한다 (apps/web/server.js)
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /repo/apps/web/public ./apps/web/public

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0

CMD ["node", "apps/web/server.js"]
