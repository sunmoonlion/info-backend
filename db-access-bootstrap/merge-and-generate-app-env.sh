#!/usr/bin/env bash
# Usage: ./merge-and-generate-app-env.sh [external|k8s|merge-only]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"
MODE="${1:-external}"
# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"
OUT="${OUT_ENV}"
APP="${SCRIPT_DIR}/../app/.env"
REF="${SCRIPT_DIR}/.env.reference"
usage() {
  printf 'Usage: %s [external|k8s|merge-only]\n' "$(basename "$0")" >&2
  printf '  external (default) — setup-external-db-access.sh + merge + .env.reference\n' >&2
  printf '  k8s — setup-k8s-db-access.sh + merge (OUT_ENV must exist)\n' >&2
  printf '  merge-only — merge + .env.reference only, no provision\n' >&2
  exit 1
}
case "${MODE}" in
  external) "${SCRIPT_DIR}/setup-external-db-access.sh" ;;
  k8s) "${SCRIPT_DIR}/setup-k8s-db-access.sh" ;;
  merge-only) : ;;
  -h|--help|help) usage ;;
  *) usage ;;
esac
[[ -f "${OUT}" ]] || { echo "[merge-and-generate-app-env] missing OUT: ${OUT}" >&2; exit 1; }
[[ -f "${APP}" ]] || { echo "[merge-and-generate-app-env] missing ${APP}" >&2; exit 1; }
get_val() {
  local k="$1" line
  line="$(grep -E "^${k}=" "${OUT}" | tail -n1)" || true
  [[ -n "${line}" ]] || return 1
  printf '%s\n' "${line#*=}"
}
set_kv_in_app() {
  local k="$1" v="$2" esc
  esc="$(printf '%s\n' "$v" | sed -e 's/[\/&|]/\\&/g')"
  if grep -qE "^${k}=" "${APP}"; then
    sed -i.bak "s#^${k}=.*#${k}=${esc}#" "${APP}" && rm -f "${APP}.bak"
  else
    printf '\n%s=%s\n' "$k" "$v" >> "${APP}"
  fi
}
for key in SQLALCHEMY_DATABASE_URI REDIS_HOST REDIS_PORT REDIS_DB REDIS_PASSWORD REDIS_USER; do
  val="$(get_val "${key}" 2>/dev/null)" || continue
  [[ -z "${val}" ]] && continue
  if [[ "${key}" == REDIS_USER ]] && [[ "${val}" =~ ^[[:space:]]*$ ]]; then
    continue
  fi
  set_kv_in_app "${key}" "${val}"
done
if ! grep -qE '^REDIS_USER=' "${OUT}"; then
  sed -i.bak '/^REDIS_USER=/d' "${APP}" && rm -f "${APP}.bak" || true
fi
{
  printf '# merge-and-generate-app-env.sh mode=%s\n' "${MODE}"
  printf '# Source: %s UTC: %s\n' "${OUT}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '# May contain secrets.\n\n'
  grep -E '^(APP_ENV|DATABASE_URL|SQLALCHEMY_DATABASE_URI|REDIS_)' "${OUT}" || true
} > "${REF}"
printf '[merge-and-generate-app-env] merged -> %s; wrote %s\n' "${APP}" "${REF}"
