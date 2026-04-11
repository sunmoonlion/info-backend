#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_K8S_CONFIG:-${CONFIG_DIR}/postgresql.k8s.env}"
REDIS_CONFIG="${REDIS_K8S_CONFIG:-${CONFIG_DIR}/redis.k8s.env}"
MONGO_CONFIG="${MONGO_K8S_CONFIG:-${CONFIG_DIR}/mongodb.k8s.env}"

log() { printf '[db-access-bootstrap][k8s] %s\n' "$*"; }
die() { printf '[db-access-bootstrap][k8s][error] %s\n' "$*" >&2; exit 1; }

bool_true() {
  case "${1:-false}" in 1|true|TRUE|yes|YES|on|ON) return 0 ;; *) return 1 ;; esac
}

require_file() { [[ -f "$1" ]] || die "Missing file: $1"; }

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  command -v kubectl >/dev/null 2>&1 || die "Missing kubectl"

  bool_true "${ENABLE_POSTGRESQL:-false}" && require_file "${PG_CONFIG}" && "${DBCTL_BIN}" --config "${PG_CONFIG}" --target k8s --action provision
  bool_true "${ENABLE_MONGODB:-false}" && require_file "${MONGO_CONFIG}" && "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target k8s --action provision

  # Redis: 支持 REDIS_AUTH_ONLY=true（不创建 ACL 用户，只做连通检查+写 Secret）。
  bool_true "${ENABLE_REDIS:-false}" && require_file "${REDIS_CONFIG}" && "${DBCTL_BIN}" --config "${REDIS_CONFIG}" --target k8s --action provision

  log "Done. k8s secrets applied in namespace: ${NAMESPACE}"
}

main "$@"
