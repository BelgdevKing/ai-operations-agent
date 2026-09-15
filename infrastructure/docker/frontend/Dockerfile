# =============================================================================
# Frontend - Next.js with TypeScript
#
# Two shipped targets from one file, plus the builder that feeds the second:
#
#   development   next dev against bind-mounted source, hot reload
#   builder       npm run build, producing .next/standalone  (not shipped)
#   production    node server.js, no npm tree, no toolchain
#
# Built with an explicit target from each Compose file:
#
#   docker compose build frontend                            -> development
#   docker compose -f docker-compose.prod.yml build frontend -> production
#
# `production` is last, so a bare `docker build` with no --target produces the
# production image rather than accidentally shipping the development one.
#
# -- NEXT_PUBLIC_* IS BAKED IN AT BUILD TIME ----------------------------------
#
# Next.js substitutes NEXT_PUBLIC_* values into the browser bundle while it
# builds. They are therefore BUILD ARGUMENTS here, not runtime environment: a
# published image points at whatever API address it was built with, and pointing
# a deployment somewhere else means building the image again.
#
#   docker compose -f docker-compose.prod.yml build \
#     --build-arg NEXT_PUBLIC_API_URL=https://api.example.test frontend
#
# Nothing secret may be passed this way. A build argument under NEXT_PUBLIC_
# ends up as a string literal in JavaScript that every visitor downloads, and it
# is also recorded in the image's build history. Addresses and labels only.
#
# Build context is ./frontend (see either Compose file).
# =============================================================================

# -- Shared base ---------------------------------------------------------------
FROM node:22-alpine AS base

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app


# -- Development ---------------------------------------------------------------
FROM base AS development

ENV NODE_ENV=development

# Dependencies first so edits to application code do not invalidate this layer.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# Create .next up front: docker-compose.yml masks it with an anonymous volume,
# which inherits ownership from the image path. Without this it lands root-owned
# and `next dev` cannot write to it.
RUN mkdir -p /app/.next

# The node image ships an unprivileged `node` user (uid 1000).
RUN chown -R node:node /app
USER node

EXPOSE 3000

# next dev compiles the first route on demand, hence the generous start period.
HEALTHCHECK --interval=10s --timeout=10s --start-period=60s --retries=12 \
    CMD wget --quiet --spider http://localhost:3000/ || exit 1

CMD ["npm", "run", "dev"]


# -- Builder -------------------------------------------------------------------
# Never shipped. Everything expensive and everything with a toolchain in it
# happens here, and only three directories cross into the runtime image.
FROM base AS builder

ENV NODE_ENV=production

COPY package.json package-lock.json* ./
# The full tree including devDependencies: TypeScript, Tailwind and the Next
# compiler are build tools, and `npm ci` with NODE_ENV=production would skip
# exactly the packages the build needs.
RUN npm ci --include=dev

COPY . .

# Defaulted so the image always builds; override them for any real deployment.
# An empty value would be worse than a wrong one - src/lib/config.ts treats an
# empty API address as "not configured" for that reason.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_APP_NAME="AI Operations Agent Platform"
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ENV NEXT_PUBLIC_APP_NAME=${NEXT_PUBLIC_APP_NAME}

RUN npm run build


# -- Production ----------------------------------------------------------------
FROM base AS production

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Three copies, and nothing else. `output: "standalone"` in next.config.ts
# traced what the server imports and wrote it here with its own server.js, so
# this image has no node_modules tree, no build cache, no source and no
# toolchain - which is a smaller image and a much shorter list of things to
# patch when somebody publishes an advisory.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node

EXPOSE 3000

# The server is compiled, so the first response is quick; the start period only
# has to cover process start.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD wget --quiet --spider http://127.0.0.1:3000/ || exit 1

# server.js is what `output: "standalone"` generated. It reads PORT and
# HOSTNAME from the environment, set above. Exec form, so the process is PID 1
# and receives SIGTERM directly - a shell wrapper would swallow it and turn
# every deploy into a ten-second kill.
CMD ["node", "server.js"]
