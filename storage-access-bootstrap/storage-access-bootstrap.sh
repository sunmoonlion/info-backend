#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config/common.env"

log() {
  printf '[storage-access-bootstrap][admin-backend] %s\n' "$*"
}

die() {
  printf '[storage-access-bootstrap][admin-backend][error] %s\n' "$*" >&2
  exit 1
}

bool_true() {
  case "${1:-false}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

usage() {
  cat <<EOF
Usage:
  $0 validate
  $0 provision
  $0 status
  $0 rotate
  $0 teardown
EOF
}

main() {
  local action="${1:-}"
  [[ -f "$CONFIG_FILE" ]] || die "Missing config: $CONFIG_FILE"
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"

  case "$action" in
    validate|provision|status|rotate|teardown) ;;
    *)
      usage
      return 1
      ;;
  esac

  [[ -f "$OBJECT_STORAGE_DECLARATION" ]] ||
    die "Missing declaration: $OBJECT_STORAGE_DECLARATION"
  [[ -x "$OBJECT_STORAGE_PROVISIONER_BIN" ]] ||
    die "Provisioner is not executable: $OBJECT_STORAGE_PROVISIONER_BIN"

  if [[ "$action" != "validate" ]] &&
    ! bool_true "${ENABLE_OBJECT_STORAGE:-false}"; then
    die "Object storage is disabled. Set ENABLE_OBJECT_STORAGE=true after assigning this Backend an object-data responsibility."
  fi

  log "action=${action} cluster=${OBJECT_STORAGE_CLUSTER} declaration=${OBJECT_STORAGE_DECLARATION}"
  "$OBJECT_STORAGE_PROVISIONER_BIN" \
    --cluster "$OBJECT_STORAGE_CLUSTER" \
    "$action" "$OBJECT_STORAGE_DECLARATION"
}

main "$@"
