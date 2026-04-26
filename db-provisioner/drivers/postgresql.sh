#!/usr/bin/env bash

pg_validate() {
  require_non_empty "APP_DB_NAME" "${APP_DB_NAME:-}"
  require_non_empty "APP_DB_USER" "${APP_DB_USER:-}"
  require_non_empty "APP_DB_PASSWORD" "${APP_DB_PASSWORD:-}"
  require_non_empty "DB_HOST" "${DB_HOST:-}"
  require_non_empty "DB_PORT" "${DB_PORT:-}"
  require_non_empty "PG_ADMIN_USER" "${PG_ADMIN_USER:-}"
  require_non_empty "PG_ADMIN_PASSWORD" "${PG_ADMIN_PASSWORD:-}"
}

pg_provision() {
  log "Provision PostgreSQL: db=${APP_DB_NAME}, user=${APP_DB_USER}"
  if bool_true "${DRY_RUN:-false}"; then
    log "DRY_RUN=true, skip executing psql"
    return 0
  fi

  require_cmd "psql"
  require_cmd "createdb"
  wait_k8s_pods_ready
  pg_precheck

  local admin_db="${PG_ADMIN_DB:-postgres}"
  local sslmode="${PG_SSLMODE:-prefer}"
  local db_exists role_exists

  export PGPASSWORD="${PG_ADMIN_PASSWORD}"

  db_exists="$(psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -tAc "SELECT 1 FROM pg_database WHERE datname='${APP_DB_NAME}'" || true)"
  if [[ "${db_exists}" != "1" ]]; then
    createdb -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" "${APP_DB_NAME}"
    log "[pg] created database: ${APP_DB_NAME}"
  else
    log "[pg] database already exists: ${APP_DB_NAME}"
  fi

  role_exists="$(psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -tAc "SELECT 1 FROM pg_roles WHERE rolname='${APP_DB_USER}'" || true)"
  if [[ "${role_exists}" != "1" ]]; then
    psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -v ON_ERROR_STOP=1 -c "CREATE ROLE \"${APP_DB_USER}\" LOGIN PASSWORD '${APP_DB_PASSWORD}';"
    log "[pg] created role: ${APP_DB_USER}"
  else
    psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -v ON_ERROR_STOP=1 -c "ALTER ROLE \"${APP_DB_USER}\" WITH PASSWORD '${APP_DB_PASSWORD}';"
    log "[pg] updated role password: ${APP_DB_USER}"
  fi

  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -v ON_ERROR_STOP=1 <<EOF
GRANT CONNECT ON DATABASE "${APP_DB_NAME}" TO "${APP_DB_USER}";
\c "${APP_DB_NAME}";
GRANT USAGE, CREATE ON SCHEMA public TO "${APP_DB_USER}";
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public TO "${APP_DB_USER}";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES TO "${APP_DB_USER}";
EOF

  APP_DB_URI="postgresql://${APP_DB_USER}:${APP_DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${APP_DB_NAME}?sslmode=${sslmode}"
  require_non_empty "APP_DB_URI(pg)" "${APP_DB_URI}"
}

pg_deprovision() {
  log "Deprovision PostgreSQL: db=${APP_DB_NAME}, user=${APP_DB_USER}"
  if bool_true "${DRY_RUN:-false}"; then
    log "DRY_RUN=true, skip executing psql"
    APP_DB_URI=""
    return 0
  fi

  require_cmd "psql"
  wait_k8s_pods_ready
  pg_precheck
  if bool_true "${DEPROVISION_DROP_DATABASE:-false}"; then
    require_cmd "dropdb"
  fi
  local admin_db="${PG_ADMIN_DB:-postgres}"
  export PGPASSWORD="${PG_ADMIN_PASSWORD}"

  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -v ON_ERROR_STOP=1 <<EOF
REVOKE CONNECT ON DATABASE "${APP_DB_NAME}" FROM "${APP_DB_USER}";
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${APP_DB_USER}') THEN
    REASSIGN OWNED BY "${APP_DB_USER}" TO "${PG_ADMIN_USER}";
    DROP OWNED BY "${APP_DB_USER}";
    DROP ROLE "${APP_DB_USER}";
  END IF;
END
\$\$;
EOF
  log "[pg] dropped role if exists: ${APP_DB_USER}"

  if bool_true "${DEPROVISION_DROP_DATABASE:-false}"; then
    psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${admin_db}" -v ON_ERROR_STOP=1 <<EOF
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname='${APP_DB_NAME}' AND pid <> pg_backend_pid();
EOF
    dropdb -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" --if-exists "${APP_DB_NAME}"
    log "[pg] dropped database if exists: ${APP_DB_NAME}"
  fi
  APP_DB_URI=""
}

pg_precheck() {
  local timeout="${DB_PRECHECK_TIMEOUT_SECONDS:-60}"
  local interval="${DB_PRECHECK_INTERVAL_SECONDS:-3}"

  if ! precheck_enabled; then
    log "PostgreSQL precheck disabled"
    return 0
  fi

  require_cmd "pg_isready"
  log "PostgreSQL precheck: waiting for readiness (timeout=${timeout}s, interval=${interval}s)"
  if wait_until "${timeout}" "${interval}" pg_isready -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" >/dev/null 2>&1; then
    log "PostgreSQL precheck passed"
    return 0
  fi

  die "PostgreSQL precheck failed: service not ready or unreachable"
}
