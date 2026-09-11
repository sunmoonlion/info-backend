#!/usr/bin/env bash
# Local helpers must not overwrite released image aliases (R5).
require_development_tag() {
    case "$1" in
        1.0.0|2.0.0)
            printf '%s\n' "Protected release tag: $1; use a development tag." >&2
            return 1
            ;;
    esac
}
