# =============================================================================
# Frontend - Next.js with TypeScript
#
# Development image: runs `next dev` against the bind-mounted source in
# docker-compose.yml. node_modules is kept in an anonymous volume so the
# container keeps the Linux-built dependencies rather than the host's.
#
# Build context is ./frontend (see docker-compose.yml).
# =============================================================================
FROM node:22-alpine

ENV NODE_ENV=development \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

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
