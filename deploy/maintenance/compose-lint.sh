#!/bin/bash
# Pre-commit/CI guard на антипаттерны в docker-compose файлах (см. ADR-014).
# Падает с ошибкой если находит запрещённое.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

ERRORS=0

# 1. Named volumes в top-level — запрещены
for f in docker-compose*.yaml; do
    [ -f "$f" ] || continue
    if grep -qE '^volumes:\s*$' "$f" && ! grep -qE '^volumes:\s*\{\s*\}\s*$' "$f"; then
        # Есть top-level volumes: блок — но он может быть пустым
        if awk '/^volumes:/{flag=1; next} /^[a-z]/{flag=0} flag && /^  [a-z]/' "$f" | grep -q .; then
            echo "[compose-lint] ERROR in $f: top-level named volumes запрещены (см. ADR-014)" >&2
            ERRORS=$((ERRORS+1))
        fi
    fi
done

# 2. docker compose down -v в скриптах
if grep -rnE 'docker( |-)compose .*down .*-v\b' . \
       --include='*.sh' --include=Makefile 2>/dev/null \
       | grep -v compose-lint.sh | grep -v PERSISTENT_PATHS.md; then
    echo "[compose-lint] ERROR: 'docker compose down -v' использовать запрещено (см. ADR-014)" >&2
    ERRORS=$((ERRORS+1))
fi

# 3. docker volume prune в скриптах (не в комментариях и не в echo)
# Игнорируем сам линтер, документацию, prune.sh (там есть пояснительный комментарий)
# и init-host.sh (там echo-предупреждение пользователю).
VOLUME_PRUNE_HITS=$(grep -rnE 'docker volume prune' . \
       --include='*.sh' --include=Makefile 2>/dev/null \
       | grep -vE 'compose-lint\.sh|PERSISTENT_PATHS\.md|prune\.sh|init-host\.sh' \
       | grep -vE '^\s*#|echo .*docker volume prune' || true)
if [ -n "$VOLUME_PRUNE_HITS" ]; then
    echo "[compose-lint] ERROR: 'docker volume prune' использовать запрещено (см. ADR-014):" >&2
    echo "$VOLUME_PRUNE_HITS" >&2
    ERRORS=$((ERRORS+1))
fi

if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "[compose-lint] FAILED ($ERRORS error(s))" >&2
    exit 1
fi

echo "[compose-lint] OK"
