#!/usr/bin/env bash

external_write_output() {
  local out="${OUTPUT_ENV_FILE:-/tmp/${SERVICE_NAME:-service}-${DB_ENGINE}.env}"
  local out_dir
  out_dir="$(dirname "$out")"
  mkdir -p "$out_dir"

  if [[ "${ACTION:-provision}" == "deprovision" ]]; then
    if bool_true "${DRY_RUN:-false}"; then
      log "DRY_RUN=true, will remove external output env: $out"
      return 0
    fi
    rm -f "$out"
    log "Removed external output env if exists: $out"
    return 0
  fi

  cat >"$out" <<EOF
SERVICE_NAME=${SERVICE_NAME:-}
ENVIRONMENT=${ENVIRONMENT:-}
DB_ENGINE=${DB_ENGINE}
DB_HOST=${DB_HOST:-}
DB_PORT=${DB_PORT:-}
APP_DB_NAME=${APP_DB_NAME:-}
APP_DB_USER=${APP_DB_USER:-}
APP_DB_PASSWORD=${APP_DB_PASSWORD:-}
APP_DB_URI=${APP_DB_URI:-}
OUTPUT_GENERATED_AT=$(now_rfc3339)
EOF

  log "Wrote external output env: $out"
}
