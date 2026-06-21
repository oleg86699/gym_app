# SvelteKit UI: dev (vite HMR) + prod (nginx со статикой).
# В этапе 1 используется только target `dev`. `prod` подъедет в 1.5.

# ─── deps ──────────────────────────────────────────────────────────────
FROM node:22-alpine AS deps

WORKDIR /app
COPY ui/package.json ui/package-lock.json* /app/

# package-lock.json при первом запуске может отсутствовать — npm install
# его создаст. После первого успешного билда положи в репо.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

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
