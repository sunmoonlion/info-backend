#!/bin/bash

# Resolve the Harbor registry used for pushing app images.
# Build itself is cluster-neutral; only the push target depends on CLUSTER.

normalize_harbor_cluster() {
    local cluster="${1:-}"
    cluster="$(printf '%s' "$cluster" | tr '[:lower:]' '[:upper:]')"
    if [[ "$cluster" =~ ^[0-9]+$ ]]; then
        cluster="C${cluster}"
    fi
    printf '%s\n' "$cluster"
}

prompt_harbor_cluster() {
    local cluster=""
    echo "请选择镜像推送目标集群：" >&2
    echo "  C1/C2/C3 : 远程 Harbor (harbor.sunmoonai.com)" >&2
    echo "  KIND     : 本地 Kind Harbor (harbor.sunmoonai.com:30443)" >&2
    printf "输入集群 [C1/C2/C3/KIND，默认 C1]: " >&2
    read -r cluster
    cluster="${cluster:-C1}"
    normalize_harbor_cluster "$cluster"
}

resolve_harbor_registry_for_push() {
    local configured_registry="${1:-}"
    local cluster="${CLUSTER:-}"

    if [[ -z "$cluster" ]]; then
        if [[ -t 0 ]]; then
            cluster="$(prompt_harbor_cluster)"
        else
            echo "CLUSTER 未设置，非交互模式下无法选择推送目标；请设置 CLUSTER=KIND 或 CLUSTER=C1" >&2
            return 1
        fi
    else
        cluster="$(normalize_harbor_cluster "$cluster")"
    fi

    case "$cluster" in
        KIND)
            export CLUSTER="KIND"
            printf '%s\n' "${KIND_HARBOR_REGISTRY:-harbor.sunmoonai.com:30443}"
            ;;
        C[0-9]*)
            export CLUSTER="$cluster"
            printf '%s\n' "${REMOTE_HARBOR_REGISTRY:-${configured_registry:-harbor.sunmoonai.com}}"
            ;;
        *)
            echo "无效 CLUSTER: $cluster，应为 KIND 或 C1/C2/C3" >&2
            return 1
            ;;
    esac
}

load_harbor_credentials_for_push() {
    local config_files=(
        "$HOME/k8s/sunmoonai/deploy-sunmoonai-all/deploy-sunmoonai-all.conf"
        "$HOME/k8s/sunmoonai/kind-infrastructure/deploy-kind/deploy-kind.conf"
        "$HOME/k8s/sunmoonai/cicd-platform/harbor/deploy-harbor/secrets/harbor-secret/deploy-harbor-secret/deploy-harbor-secret.conf"
        "$HOME/k8s/sunmoonai/cicd-platform/harbor/utils/harbor-image-management/harbor-image.conf"
    )

    local file
    for file in "${config_files[@]}"; do
        [[ -f "$file" ]] || continue
        # shellcheck source=/dev/null
        source "$file" 2>/dev/null || true
    done

    local user="${HARBOR_USER:-${HARBOR_USERNAME:-${HARBOR_ADMIN_USERNAME:-${HARBOR_ADMIN_USER:-admin}}}}"
    local pass="${HARBOR_PASSWORD:-${HARBOR_ADMIN_PASSWORD:-}}"

    if [[ -z "$user" ]]; then
        read -rp "Harbor 用户名: " user
    fi
    if [[ -z "$pass" ]]; then
        read -rsp "Harbor 密码: " pass
        echo
    fi

    HARBOR_USER="$user"
    HARBOR_PASSWORD="$pass"
    export HARBOR_USER HARBOR_PASSWORD
}

harbor_artifact_exists_for_push() {
    local full_image="$1"
    local registry="${full_image%%/*}"
    local remainder="${full_image#*/}"
    local project="${remainder%%/*}"
    local repo_and_tag="${remainder#*/}"
    local repo="${repo_and_tag%:*}"
    local tag="${repo_and_tag##*:}"

    if [[ -z "$registry" || -z "$project" || -z "$repo" || "$repo" == "$repo_and_tag" || "$tag" == "$repo_and_tag" ]]; then
        return 1
    fi

    local repo_encoded
    repo_encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$repo")" || return 1

    local http_code
    http_code="$(curl -sk -o /dev/null -w "%{http_code}" \
        -u "${HARBOR_USER}:${HARBOR_PASSWORD}" \
        "https://${registry}/api/v2.0/projects/${project}/repositories/${repo_encoded}/artifacts/${tag}")"
    [[ "$http_code" == "200" ]]
}

push_image_with_harbor_verify() {
    local runtime_cmd="$1"
    local full_image="$2"
    local retry_count="${PUSH_RETRY_COUNT:-2}"
    local retry_delay="${PUSH_RETRY_DELAY:-3}"
    local attempt

    for ((attempt=1; attempt<=retry_count; attempt++)); do
        if $runtime_cmd push "$full_image"; then
            return 0
        fi

        if harbor_artifact_exists_for_push "$full_image"; then
            echo "Harbor 中已存在目标镜像，按推送成功处理: $full_image"
            return 0
        fi

        if (( attempt < retry_count )); then
            echo "推送失败，${retry_delay}s 后重试 (${attempt}/${retry_count}): $full_image" >&2
            sleep "$retry_delay"
        fi
    done

    return 1
}
