#!/usr/bin/env bash
#
# Validate that GitHub Actions can SSH into each target server AND that the
# server has docker + docker compose installed and accessible. Runs as the
# first job in deploy-prod.yml — fail fast before building images.
#
# Targets are chosen by $GITHUB_REF_NAME:
#   main → PROD_A (+ PROD_B, PROD_C, … when their secrets are set)
#
# Each target needs: <LABEL>_SSH_HOST, _SSH_USER, _SSH_PORT (default 22),
# _SSH_AUTH (the full private key text).

set -euo pipefail
failures=0

check_target() {
  local label="$1" host="$2" user="$3" auth_blob="$4" port="${5:-22}"
  local missing=()
  [[ -n "${host}" ]] || missing+=("${label}_SSH_HOST")
  [[ -n "${user}" ]] || missing+=("${label}_SSH_USER")
  [[ -n "${auth_blob}" ]] || missing+=("${label}_SSH_AUTH")
  if (( ${#missing[@]} > 0 )); then
    echo "::error::Missing required values: ${missing[*]}"; failures=$((failures + 1)); return
  fi

  local auth_normalized="${auth_blob//$'\r'/}"; auth_normalized="${auth_normalized//\\n/$'\n'}"
  local auth_file="${RUNNER_TEMP:-/tmp}/${label,,}_ssh_auth"
  printf '%s\n' "${auth_normalized}" > "${auth_file}"; chmod 600 "${auth_file}"

  if [[ "${auth_normalized}" =~ ^ssh-(rsa|ed25519|ecdsa)[[:space:]] ]]; then
    echo "::error::${label}_SSH_AUTH looks like a public key (.pub). Use the PRIVATE key in secrets."
    failures=$((failures + 1)); return
  fi
  if ! ssh-keygen -y -f "${auth_file}" >/dev/null 2>&1; then
    echo "::error::${label}_SSH_AUTH is invalid or passphrase-protected. Use an unencrypted deploy key."
    failures=$((failures + 1)); return
  fi
  if ! timeout 10 bash -c "</dev/tcp/${host}/${port}" >/dev/null 2>&1; then
    echo "::error::TCP check failed for ${label} (${host}:${port}). Port closed/unreachable."
    failures=$((failures + 1)); return
  fi

  local ssh_opts=(-i "${auth_file}" -p "${port}" -o BatchMode=yes -o IdentitiesOnly=yes
                  -o PasswordAuthentication=no -o StrictHostKeyChecking=no
                  -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30)
  local out=""
  if ! out="$(ssh "${ssh_opts[@]}" "${user}@${host}" "echo connected" 2>&1)"; then
    echo "::error::SSH check failed for ${label} (${user}@${host}:${port}): ${out}"
    failures=$((failures + 1)); return
  fi
  if ! out="$(ssh "${ssh_opts[@]}" "${user}@${host}" \
      "command -v docker >/dev/null 2>&1 || { echo __MISSING_DOCKER__; exit 3; }; \
       docker info >/dev/null 2>&1 || { echo __NO_DOCKER_PERMS__; exit 5; }; \
       docker compose version >/dev/null 2>&1 || { echo __MISSING_COMPOSE__; exit 4; }; \
       echo __PREREQ_OK__" 2>&1)"; then
    case "${out}" in
      *__MISSING_DOCKER__*)  echo "::error::${label}: docker not installed. Run deploy/init-host.sh.";;
      *__NO_DOCKER_PERMS__*) echo "::error::${label}: docker daemon not accessible for ${user}. usermod -aG docker ${user}.";;
      *__MISSING_COMPOSE__*) echo "::error::${label}: docker compose plugin missing.";;
      *)                     echo "::error::${label}: prereq failure: ${out}";;
    esac
    failures=$((failures + 1)); return
  fi
  echo "SSH preflight passed for ${label} (${user}@${host}:${port})."
}

if [[ "${GITHUB_REF_NAME:-}" == "main" ]]; then
  check_target "PROD_A" "${PROD_A_SSH_HOST:-}" "${PROD_A_SSH_USER:-}" "${PROD_A_SSH_AUTH:-}" "${PROD_A_SSH_PORT:-22}"
  # PROD_B проверяется только когда заданы его секреты (иначе пропускаем).
  if [[ -n "${PROD_B_SSH_HOST:-}" ]]; then
    check_target "PROD_B" "${PROD_B_SSH_HOST}" "${PROD_B_SSH_USER}" "${PROD_B_SSH_AUTH}" "${PROD_B_SSH_PORT:-22}"
  fi
else
  echo "::error::Unsupported branch for deploy: ${GITHUB_REF_NAME:-<unset>}"; failures=$((failures + 1))
fi

(( failures > 0 )) && exit 1 || exit 0
