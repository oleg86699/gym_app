# SvelteKit UI: dev (vite HMR) + prod (nginx со статикой).
# В этапе 1 используется только target `dev`. `prod` подъедет в 1.5.

# ─── deps ──────────────────────────────────────────────────────────────
FROM node:22-alpine AS deps

WORKDIR /app
COPY ui/package.json ui/package-lock.json* /app/

# npm имеет давний баг: lockfile, сгенерённый на одной OS/arch, не содержит
# OPTIONAL-зависимости других платформ (например @rollup/rollup-linux-x64-gnu).
# Тогда `vite build` в CI падает с "Cannot find module .../rollup/dist/native.js".
# Поэтому НЕ используем `npm ci` со «своим» локом — сносим лок и ставим свежо
# под платформу сборки (linux/amd64 в CI). Чуть теряем детерминизм, зато
# кросс-платформенно надёжно.
RUN rm -f package-lock.json && npm install --no-audit --no-fund

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
