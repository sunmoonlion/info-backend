#!/usr/bin/env bash
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SELF_DIR}/.." && pwd)"
TEMPLATE_DIR="${ROOT_DIR}/templates/db-access-bootstrap-template"

TARGET_DIR=""
SERVICE_NAME=""
NAMESPACE=""
PG_DB_NAME=""
PG_DB_USER=""
PG_DB_PASSWORD=""
MONGO_DB_NAME=""
MONGO_DB_USER=""
MONGO_DB_PASSWORD=""

usage() {
  cat <<'EOF'
init-service-provision-template.sh - scaffold db-access-bootstrap for one service

Usage:
  init-service-provision-template.sh \
    --target-dir /home/zym/app/your-service \
    --service-name your-service \
    --namespace your-k8s-namespace \
    --pg-db-name your_db \
    --pg-db-user your_user \
    --pg-db-password your_password

Optional mongo placeholders:
  --mongo-db-name xxx --mongo-db-user xxx --mongo-db-password xxx
EOF
}

die() {
  printf '[init-template][error] %s\n' "$*" >&2
  exit 1
}

require_non_empty() {
  [[ -n "${2:-}" ]] || die "Missing required arg: $1"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --target-dir) TARGET_DIR="${2:-}"; shift 2 ;;
      --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
      --namespace) NAMESPACE="${2:-}"; shift 2 ;;
      --pg-db-name) PG_DB_NAME="${2:-}"; shift 2 ;;
      --pg-db-user) PG_DB_USER="${2:-}"; shift 2 ;;
      --pg-db-password) PG_DB_PASSWORD="${2:-}"; shift 2 ;;
      --mongo-db-name) MONGO_DB_NAME="${2:-}"; shift 2 ;;
      --mongo-db-user) MONGO_DB_USER="${2:-}"; shift 2 ;;
      --mongo-db-password) MONGO_DB_PASSWORD="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown arg: $1" ;;
    esac
  done
}

replace_tokens() {
  local f="$1"
  sed -i "s#__SERVICE_NAME__#${SERVICE_NAME}#g" "$f"
  sed -i "s#__SERVICE_DIR__#${TARGET_DIR}#g" "$f"
  sed -i "s#__NAMESPACE__#${NAMESPACE:-default}#g" "$f"
  sed -i "s#__PG_DB_NAME__#${PG_DB_NAME}#g" "$f"
  sed -i "s#__PG_DB_USER__#${PG_DB_USER}#g" "$f"
  sed -i "s#__PG_DB_PASSWORD__#${PG_DB_PASSWORD}#g" "$f"
  sed -i "s#__MONGO_DB_NAME__#${MONGO_DB_NAME:-${SERVICE_NAME}_db}#g" "$f"
  sed -i "s#__MONGO_DB_USER__#${MONGO_DB_USER:-${SERVICE_NAME}}#g" "$f"
  sed -i "s#__MONGO_DB_PASSWORD__#${MONGO_DB_PASSWORD:-change_me}#g" "$f"
}

main() {
  parse_args "$@"
  require_non_empty "--target-dir" "${TARGET_DIR}"
  require_non_empty "--service-name" "${SERVICE_NAME}"
  require_non_empty "--pg-db-name" "${PG_DB_NAME}"
  require_non_empty "--pg-db-user" "${PG_DB_USER}"
  require_non_empty "--pg-db-password" "${PG_DB_PASSWORD}"

  [[ -d "${TARGET_DIR}" ]] || die "Target dir not found: ${TARGET_DIR}"
  [[ -d "${TEMPLATE_DIR}" ]] || die "Template dir not found: ${TEMPLATE_DIR}"

  local out="${TARGET_DIR}/db-access-bootstrap"
  rm -rf "${out}"
  mkdir -p "${out}"
  cp -r "${TEMPLATE_DIR}/." "${out}/"

  while IFS= read -r -d '' file; do
    replace_tokens "$file"
  done < <(find "${out}" -type f -print0)

  chmod +x "${out}/setup-external-db-access.sh" "${out}/teardown-external-db-access.sh" "${out}/setup-k8s-db-access.sh" "${out}/teardown-k8s-db-access.sh"

  printf '[init-template] created: %s\n' "${out}"
  printf '[init-template] next step: cd "%s" && ./db-access-bootstrap/setup-external-db-access.sh\n' "${TARGET_DIR}"
}

main "$@"
