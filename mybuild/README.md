# info-backend 镜像构建

## 架构

- **构建上下文**：子模块根目录（`info-backend/`）
- **源码位置**：`app/` 子目录（pyproject.toml、uv.lock、应用代码）
- **构建方式**：多阶段构建（python:slim 安装依赖 → python:slim 运行时）
- **镜像名称**：`info-backend:architecture-v2-dev`（本地）；CI 使用 git SHA tag

## 文件说明

| 文件 | 用途 |
|------|------|
| `Dockerfile` | 多阶段构建文件（本地 & CI 共用） |
| `build.conf` | 本地构建配置（镜像名、仓库、REGISTRY 等） |
| `build-image.sh` | 本地构建（可选推送）脚本 |
| `push-image.sh` | 单独推送脚本 |
| `rebuild-and-run.sh` | 快速重建并本地运行 |

## 本地构建（黄金命令）

```bash
# 在子模块根目录执行
docker build -f mybuild/Dockerfile \
  --build-arg REGISTRY=harbor.sunmoonai.com:30443/k8s-images \
  -t info-backend:architecture-v2-dev .
```

## 使用脚本构建

```bash
cd mybuild
./build-image.sh             # 构建
./build-image.sh --tag candidate-<git-sha> # 自定义开发 tag（替换尖括号内容）
./push-image.sh              # 推送到 Harbor
./rebuild-and-run.sh         # 重建并本地运行（http://localhost:8000）
```

## CI（Kaniko）参数

```
--dockerfile    mybuild/Dockerfile
--context       <子模块根目录>
--build-arg     REGISTRY=harbor.sunmoonai.com:30443/k8s-images
--destination   harbor.sunmoonai.com:30443/app-images/info-backend:<git-sha>
```

## 注意事项

- API、Worker、Scheduler、Migration 共用此镜像，以各自 bootstrap 入口启动；迁移独立运行。
- `app/uv.lock` 必须提交到 Git，构建使用 `--frozen` 严格锁定依赖版本
- 本地构建无需 Harbor 时传 `--build-arg REGISTRY=docker.io/library`；空值会产生无效的 `/python` 镜像路径。
- 开发构建与推送脚本拒绝 `1.0.0` / `2.0.0`，不读取 Harbor 凭据、不覆盖正式标签；`bash mybuild/test-release-tag.sh` 在子模块根验证该边界。
- `rebuild-and-run.sh` 会停止并移除同名本地开发容器，执行前确认其不是需要保留的运行实例。
