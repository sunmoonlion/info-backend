#!/usr/bin/env bash
# 证券采集联网冒烟（0008-info 段一、段二，MVP-01 至 MVP-04）：真实网站、真实数据库、本地对象存储。
# 采两次同一个代码，从磁盘上的文件复算每条原文的校验值，再从批次建两次数据集。
# 可选：DATASET_OUT=<路径> 把建出的数据集文件另存一份。
#
# 用法：在 info-backend/app 下
#   DATABASE_URL=postgresql://<用户>:<口令>@127.0.0.1:<端口>/<库> \
#   STORAGE_LOCAL_ROOT=$HOME/info-smoke-storage \
#   bash scripts/security_ingest_smoke.sh 600009
# 前提：库已建好并装了 uuid-ossp；脚本自己跑迁移。库与存储目录应是一次性的。
# 输出不含口令；结果写 scripts/results/security-ingest-smoke.<时间>.txt。
set -uo pipefail
CODE="${1:?用法: security_ingest_smoke.sh <六位证券代码>}"
: "${DATABASE_URL:?要设 DATABASE_URL}" "${STORAGE_LOCAL_ROOT:?要设 STORAGE_LOCAL_ROOT}"
export STORAGE_BACKEND=local ENV="${ENV:-development}"
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
OUT="scripts/results/security-ingest-smoke.$(date +%Y%m%d-%H%M%S).txt"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
{
  echo "code=$CODE commit=$(git rev-parse --short HEAD) python=$(uv run python --version 2>&1)"
  uv run python -m app.bootstrap.migration upgrade head >/dev/null 2>&1 || { echo "迁移失败"; echo "exit=1"; exit 1; }
  echo "schema=$(uv run python -m app.bootstrap.migration current 2>/dev/null | tail -1)"
  rc=0
  for n in 1 2; do
    start=$(date +%s)
    uv run python -m app.cli.security_ingest --code "$CODE" >"$TMP/run$n.json" 2>"$TMP/run$n.err"; code=$?
    echo "== 第 $n 次 exit=$code 耗时=$(( $(date +%s) - start ))s"
    python3 - "$TMP/run$n.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("status", "items", "new_artifacts", "reused_artifacts", "bytes_fetched", "by_source",
          "report_years", "statement_periods", "error_code", "error_detail", "error", "type"):
    if k in d: print(f"   {k} = {d[k]}")
PY
    [ "$code" -eq 0 ] || rc=2
  done
  uv run python - <<'PY'
import asyncio, hashlib, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from core.config import get_settings

async def main():
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as c:
        rows = (await c.execute(text("SELECT bucket, object_key, sha256, size_bytes FROM raw_artifact WHERE artifact_type LIKE 'security_%'"))).all()
        retried = (await c.execute(text("SELECT count(*) FROM security_ingestion_item WHERE meta ? 'attempts'"))).scalar_one()
        fresh = (await c.execute(text("SELECT kind, count(*) FROM security_ingestion_item i JOIN security_ingestion b ON b.id=i.ingestion_id WHERE NOT i.reused AND b.requested_at=(SELECT max(requested_at) FROM security_ingestion) GROUP BY kind"))).all()
    await engine.dispose()
    root = os.path.expanduser(os.environ["STORAGE_LOCAL_ROOT"]); ok = bad = 0
    for bucket, key, sha, size in rows:
        path = os.path.join(root, bucket, key)
        data = open(path, "rb").read() if os.path.isfile(path) else None
        if data is not None and hashlib.sha256(data).hexdigest() == sha and len(data) == size: ok += 1
        else: bad += 1; print("   不一致或缺失:", key)
    print(f"== 复算 原文={len(rows)} 一致={ok} 不一致或缺失={bad} 重试过的条目={retried}")
    print(f"== 第二次新增的原文（按类型）: {dict(fresh)}")
    raise SystemExit(0 if bad == 0 else 3)
asyncio.run(main())
PY
  v=$?; [ "$v" -eq 0 ] || rc=$v
  # 段二：从刚采的批次建数据集，建两次，版本与文件应完全相同
  for n in 1 2; do
    start=$(date +%s)
    uv run python -m app.cli.security_dataset --code "$CODE" --out "$TMP/dataset$n.sqlite" >"$TMP/ds$n.json" 2>"$TMP/ds$n.err"; code=$?
    echo "== 建数据集 第 $n 次 exit=$code 耗时=$(( $(date +%s) - start ))s"
    python3 - "$TMP/ds$n.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("status", "dataset_id", "data_version", "sha256", "size_bytes", "row_counts", "start_date",
          "end_date", "basis_by_year", "failed_checks", "report_problems", "error", "detail", "type"):
    if k in d: print(f"   {k} = {d[k]}")
PY
    [ "$code" -eq 0 ] || rc=2
  done
  if [ -f "$TMP/dataset1.sqlite" ] && [ -f "$TMP/dataset2.sqlite" ]; then
    a=$(sha256sum "$TMP/dataset1.sqlite" | cut -d' ' -f1); b=$(sha256sum "$TMP/dataset2.sqlite" | cut -d' ' -f1)
    echo "== 两次建出的文件 sha256 相同: $([ "$a" = "$b" ] && echo 是 || echo 否)"
    [ "$a" = "$b" ] || rc=4
    [ -n "${DATASET_OUT:-}" ] && cp "$TMP/dataset1.sqlite" "$DATASET_OUT" && echo "== 数据集另存: $DATASET_OUT"
  fi
  echo "exit=$rc"
} 2>&1 | grep -v -i "password\|secret\|token=" | tee "$OUT"
echo "结果: $OUT"
