# SvelteKit UI: dev (vite HMR) + prod (nginx со статикой).
# В этапе 1 используется только target `dev`. `prod` подъедет в 1.5.

# ─── deps ──────────────────────────────────────────────────────────────
# node:22-slim (Debian/glibc), НЕ alpine/musl: на musl нативный парсер rollup
# капризничает с детектом libc. Лок коммитнут и содержит ВСЕ платформенные
# optional'ы rollup + рабочие версии svelte/vite — поэтому npm ci (детерминизм).
FROM node:22-slim AS deps

WORKDIR /app
COPY ui/package.json ui/package-lock.json* /app/

RUN npm ci --no-audit --no-fund
# Обход бага npm (npm/cli#4828): `npm ci` пропускает platform-specific optional-
# зависимости даже когда они есть в локе. rollup без нативного бинаря падает с
# "Cannot find module .../rollup/dist/native.js". Доустанавливаем нативный rollup
# РОВНО под арку текущей сборки (glibc): x64-gnu в CI (amd64), arm64-gnu локально.
RUN ARCH="$(node -p 'process.arch')" \
 && npm install --no-save "@rollup/rollup-linux-${ARCH}-gnu@$(node -p "require('rollup/package.json').version")"

# ─── dev ───────────────────────────────────────────────────────────────
FROM deps AS dev

COPY ui/ /app/

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# ─── build (prep for prod) ─────────────────────────────────────────────
FROM deps AS build

COPY ui/ /app/
RUN npm run build

# ─── prod ──────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS prod

COPY --from=build /app/build /usr/share/nginx/html
COPY docker/nginx/conf.d/ui.prod.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
