#!/usr/bin/env bash
# Deprecated: use merge-and-generate-app-env.sh merge-only (or external / k8s).
exec "$(cd "$(dirname "$0")" && pwd)/merge-and-generate-app-env.sh" merge-only
