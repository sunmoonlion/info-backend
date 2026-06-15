# storage-access-bootstrap (admin-backend)

This directory declares and provisions standard S3 access for
`info-admin-backend`.

It is disabled by default. Enable it only when this Backend owns a distinct
set of object data inside the App domain.

## Configuration

1. Review `config/access.json`.
2. Set the target Namespace, Bucket names, permissions and versioning.
3. Set `ENABLE_OBJECT_STORAGE=true` in `config/common.env`.
4. Override `SUNMOONAI_K8S_ROOT` if the `k8s` repository is not at `~/k8s`.

The declaration contains no credentials. The Data Platform provisioner creates
an IAM identity and writes these resources to the target Namespace:

```text
Secret:    info-admin-backend-s3
ConfigMap: info-admin-backend-s3
```

The Backend Deployment should reference both resources with `envFrom`.
When generating its Kubernetes deployment with `k8s-scaffold`, use:

```bash
./k8s-scaffold/scaffold.sh info-admin-backend 8001 \
  --type backend \
  --with-object-storage
```

For an existing generated deployment, set these fields in its
`generate-app.conf`:

```text
INFO_ADMIN_BACKEND_OBJECT_STORAGE_CONFIGMAP_NAME=info-admin-backend-s3
INFO_ADMIN_BACKEND_OBJECT_STORAGE_SECRET_NAME=info-admin-backend-s3
```

## Commands

```bash
./storage-access-bootstrap.sh validate
./storage-access-bootstrap.sh provision
./storage-access-bootstrap.sh status
./storage-access-bootstrap.sh rotate
./storage-access-bootstrap.sh teardown
```

Convenience wrappers:

```bash
./setup-k8s-storage-access.sh
./teardown-k8s-storage-access.sh
```

`teardown` removes the IAM identity, Policy, Secret and ConfigMap. Bucket data
is retained.
