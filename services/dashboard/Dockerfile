FROM node:20-alpine AS deps
WORKDIR /app
COPY services/dashboard/package.json services/dashboard/package-lock.json* ./
# --legacy-peer-deps: Next.js sometimes pins a peer-dep range that doesn't
# include the React stable version we want. This flag falls back to npm v6
# resolution behaviour, which doesn't error on peer mismatches.
RUN npm install --no-audit --no-fund --legacy-peer-deps

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY services/dashboard ./
# Next.js standalone output expects a public/ directory to exist even if
# we don't ship static assets. Create one so the COPY in the runtime
# stage doesn't fail.
RUN mkdir -p public
RUN npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
