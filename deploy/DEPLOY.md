# Deploy — gym_app

CI/CD: push to **`main`** → GitHub Actions builds `app`/`ui`/`backup` images to
GHCR and deploys them over SSH to every configured prod server in parallel.
Mirrors the bap_admin setup; runs on the **same server**, on a **different port**
(8090 by default) and a different docker project name (`gym_app`), so the two
stacks don't collide.

## Topology on the server

```
/opt/gym_app/
├── .env                              ← injected per deploy (secret PROD_A_APP_ENV)
├── docker-compose.yaml               ← uploaded from repo
├── deploy/docker-compose.deploy.yaml ← prod overlay (pins images, opens port)
├── docker/nginx/{nginx.conf,conf.d/default.prod.conf}  ← uploaded
├── data/                             ← postgres + minio volumes (persist)
└── backups/                          ← pg dumps
```

Only **one** public port is exposed: `${PUBLIC_PORT:-8090} → nginx:80`.
nginx routes `/admin/api/*`,`/api/v1/*` → `app:8080`, everything else → the prod
`ui` container (static SvelteKit). DB / Redis / MinIO / PgBouncer stay
docker-internal. Project name `gym_app` (containers `gym_app-*`).

Images: `ghcr.io/oleg86699/gym_app/{app,ui,backup}:<version>` (version is
auto-semver from conventional commits).

## One-time setup (you do this)

1. **Create the GitHub repo** `oleg86699/gym_app` (empty, private), then push:
   ```bash
   cd app
   git remote add origin git@github.com:oleg86699/gym_app.git
   git push -u origin main
   ```

2. **GitHub → Settings → Environments → New environment `production`.**

3. **Secrets** (Environment `production`):
   | Secret | Value |
   |---|---|
   | `PROD_A_SSH_HOST` | server IP/host (same as bap_admin) |
   | `PROD_A_SSH_USER` | deploy user (same as bap_admin) |
   | `PROD_A_SSH_KEY`  | the deploy **private** key (unencrypted; reuse bap_admin's) |
   | `PROD_A_APP_ENV`  | full prod `.env` content (see below) |

4. **Variables** (Environment `production`):
   | Var | Value |
   |---|---|
   | `PROD_A_DEPLOY_ROOT` | `/opt/gym_app` |
   | `PROD_A_PUBLIC_PORT` | `8090` |
   | `PROD_A_NGINX_BIND_IP` | `0.0.0.0` |
   | `PROD_A_SSH_PORT` | `22` (or your port) |

5. **Server**: Docker is already there (bap_admin). Just open the port and make
   sure the deploy user's authorized_keys has the GH Actions key (reuse
   bap_admin's). The workflow creates `/opt/gym_app/{data,backups,...}` itself.
   ```bash
   sudo ufw allow 8090/tcp
   ```

6. **Deploy**: push to `main`, or run the **Deploy Prod** workflow manually
   (Actions → Deploy Prod → Run workflow).

## The prod `.env` (`PROD_A_APP_ENV` secret)

Start from `.env.example`. Critical differences from local dev:
- `ENVIRONMENT=production`  (makes auth cookies Secure)
- `PUBLIC_BASE_URL=http://<server-ip>:8090`  (or your domain)
- real `POSTGRES_PASSWORD`, `JWT_SECRET` (64+ hex), `SUPER_ADMIN_PASSWORD`,
  `MINIO_ROOT_PASSWORD`, `ENCRYPTION_KEY`
- `DATABASE_URL` → points at `pgbouncer:5432`, `REDIS_URL` → `redis://redis:6379/0`,
  S3 endpoint → `http://minio:9000` (internal docker DNS).
- The `*_PORT_HOST` vars are dev-only (no host binding in prod) — leave defaults.

`deploy/preflight-env.sh` validates the required keys before every deploy.

## Adding a 2nd / 3rd prod server (deploy to many at once)

1. In `.github/workflows/deploy-prod.yml`, copy the whole `deploy-prod-a:` job,
   rename to `deploy-prod-b:`, replace `PROD_A_` → `PROD_B_` inside.
2. Add `PROD_B_*` secrets/vars in the `production` environment.
3. Uncomment the `PROD_B` block in `deploy/preflight-connections.sh`.
The jobs run in parallel — one push deploys everywhere.

## Operate (SSH on the server)

```bash
cd /opt/gym_app
C="docker compose --project-name gym_app -f docker-compose.yaml -f deploy/docker-compose.deploy.yaml"
$C ps
$C logs --tail=100 app
$C exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```
