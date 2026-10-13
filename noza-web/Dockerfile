FROM node:20-alpine AS base

# ── Install dependencies ──
FROM base AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ── Build ──
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* vars are inlined at build time.
ARG NEXT_PUBLIC_INGABE_URL=http://localhost:8000
RUN printf "NEXT_PUBLIC_INGABE_URL=%s\n" "$NEXT_PUBLIC_INGABE_URL" > .env.production

# Limit Node memory for Render starter build plan
ENV NODE_OPTIONS="--max-old-space-size=512"
RUN npm run build

# ── Production ──
FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

RUN mkdir -p ./public
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 10000
ENV PORT=10000
ENV HOSTNAME="0.0.0.0"
CMD ["node", "server.js"]
