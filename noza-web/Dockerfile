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

# NEXT_PUBLIC_* vars must exist at build time (inlined by webpack).
# Render passes env vars at runtime only, so we write .env.production
# from ARGs with sensible defaults.
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_Y2xvc2luZy1tb2NjYXNpbi0xMS5jbGVyay5hY2NvdW50cy5kZXYk
ARG NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
ARG NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
ARG NEXT_PUBLIC_INGABE_URL=https://gis.nozalabs.rw

RUN printf "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=%s\nNEXT_PUBLIC_CLERK_SIGN_IN_URL=%s\nNEXT_PUBLIC_CLERK_SIGN_UP_URL=%s\nNEXT_PUBLIC_INGABE_URL=%s\n" \
    "$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" \
    "$NEXT_PUBLIC_CLERK_SIGN_IN_URL" \
    "$NEXT_PUBLIC_CLERK_SIGN_UP_URL" \
    "$NEXT_PUBLIC_INGABE_URL" > .env.production

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
