#!/usr/bin/env bash
#
# Validate a .env file before scp'ing it to a prod server. Catches missing
# required vars and obvious placeholder values that would break the deploy.
#
# Usage: deploy/preflight-env.sh <path-to-env-file>

set -euo pipefail

[[ $# -ge 1 ]] || { echo "usage: $0 <path-to-env-file>"; exit 1; }
ENV_FILE="$1"
[[ -f "${ENV_FILE}" ]] || { echo "::error::env file not found: ${ENV_FILE}"; exit 1; }

load_env_file() {
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    line="${line#"${line%%[![:space:]]*}"}"
    [[ "${line}" == export[[:space:]]* ]] && { line="${line#export}"; line="${line#"${line%%[![:space:]]*}"}"; }
    [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || { echo "warning: skip: ${line}" >&2; continue; }
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"; value="${value//\\n/$'\n'}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    else
      value="${value%%[[:space:]]\#*}"; value="${value%"${value##*[![:space:]]}"}"
    fi
    printf -v "${key}" '%s' "${value}"; export "${key}"
  done < "${ENV_FILE}"
}
load_env_file "${ENV_FILE}"

errors=(); warnings=()
require() { [[ -n "${!1:-}" ]] || errors+=("Missing required env var: $1"); }
warn_placeholder() {
  local v="${!1:-}"; [[ -z "${v}" ]] && return 0
  if [[ "${v}" =~ (change_me|changeme|change-me|your-|example\.com|placeholder) ]]; then
    warnings+=("Placeholder still in $1: ${v}")
  fi
  return 0
}

# Hard requirements — without these the app won't boot / login won't work.
require POSTGRES_DB
require POSTGRES_USER
require POSTGRES_PASSWORD
require JWT_SECRET
require SUPER_ADMIN_USERNAME
require SUPER_ADMIN_PASSWORD

# Recommended (constructed from POSTGRES_* if absent, but explicit is safer).
[[ -n "${DATABASE_URL:-}" ]] || warnings+=("DATABASE_URL empty — ensure the app derives it or set it explicitly.")
[[ -n "${REDIS_URL:-}" ]] || warnings+=("REDIS_URL empty.")
[[ "${ENVIRONMENT:-}" != "dev" ]] || warnings+=("ENVIRONMENT=dev on a prod deploy — cookies won't be Secure. Set ENVIRONMENT=production.")

warn_placeholder POSTGRES_PASSWORD
warn_placeholder JWT_SECRET
warn_placeholder SUPER_ADMIN_PASSWORD
warn_placeholder MINIO_ROOT_PASSWORD

if (( ${#warnings[@]} > 0 )); then
  echo "Preflight warnings:"; for w in "${warnings[@]}"; do echo "  ⚠ ${w}"; done
fi
if (( ${#errors[@]} > 0 )); then
  echo "::error::Preflight FAILED (${#errors[@]} error(s)):"
  for e in "${errors[@]}"; do echo "::error::  ${e}"; done
  exit 1
fi
echo "Preflight OK: ${ENV_FILE}"
