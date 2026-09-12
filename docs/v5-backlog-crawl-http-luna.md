# v5 M1-102 / B2.1：Info 公网抓取边界

日期：2026-09-12。Luna 单人实施、自测；源码候选，不表示已部署。
旧任务追溯：k8s 的 `sunmoonai/docs/v5-backlog-disposition-luna.md`。

## 范围与判据

本子包只关闭正文抓取、RSS/Atom discovery、API discovery 三条 HTTP 出口的地址与读取边界。
M1-102 的来源并发限制另属 B2.2，不能把本子包通过解释为旧任务全部完成。

这里是 Info 采集领域专用策略：只采公网 HTTP/80、HTTPS/443，不是四仓通用的任意 HTTP
客户端。Info→Knowledge、对象存储、身份服务和其他 App 的受信内网调用不改；模板无采集
职责，因此本包不新增模板原语、不传播到无采集职责的实例。

| 判据 | 实现/测试 |
| --- | --- |
| URL 与目标安全 | 禁凭据 URL、控制字符、非标准端口、非 HTTP(S)；拒绝私网/回环/link-local/metadata、组播和 IPv6 转换地址 |
| DNS 不得校验后重新按域名连接 | 校验全部解析结果，混合公私地址拒绝；连接固定数字 IP；每跳重新解析检查 |
| TLS 保持原域名校验 | Host 与 sni_hostname 保持原域名，verify 不关闭；每跳独立 client 防止同 IP 不同 TLS 主机复用连接 |
| 跳转与凭据 | 手动逐跳处理、最多 5 跳、禁止 HTTPS 降级；跨源丢弃所有自定义敏感头，只保留三类协商头；不复用 Cookie jar |
| 资源限额 | 身份编码、逐块计数、提前检查 Content-Length；拒绝压缩响应，避免解压膨胀；DNS+跳转+读取共用总 deadline |
| DNS 退出 | libc DNS 不可取消，使用最多 4 个 daemon 查询线程；超时不会阻塞 asyncio.run 的默认 executor 收尾；进程级回归验证 |
| 环境隔离 | trust_env=False；不自动使用代理、netrc 或环境 CA；可信服务调用保持原样 |
| 持久失败与重复执行 | 拒绝目标写 crawl_job failed、不写 Artifact；完成任务重投不抓取；取消/租约丢失不误写终态 |

兼容性变化明确：依赖私网来源、非标准端口、环境代理、环境 CA、压缩强制响应或跨站凭据
传递的采集将失败关闭。不能用临时白名单或关闭 TLS 绕过；确需内网采集时另做受权来源策略。
公网第一 IP 不可达时本次失败，不自动尝试未校验目标。这里不承诺抓取服务无限可用。

## 验证与未覆盖边界

- Ruff、Pyright 通过。
- 专项网络边界使用 MockTransport 进行故障注入，不冒充真实互联网负向测试。
- DNS 阻塞退出由真正 Python 子进程验证，不只是模拟异步 sleep。
- 一次真实 HTTPS `example.com` 只读探针返回 200；原域名保留、559 bytes。
- 一次性 PostgreSQL 17.6，数据库 `info_backlog_tests`，随机 schema；不接业务库。
- 全量套件与共享 web-interaction 向量最终 **186 passed / 0 skipped**（6.77 秒）；
  包括 57 项网络边界测试与真实 PostgreSQL 故障回归。没有跑到的门禁不销账。
- 未部署 KIND/云端业务运行面，未 build/push 镜像，不改正式 release。
- 暂未验证真实 TLS 错证书矩阵、跨源公网重定向或来源并发；对应单元注入与剩余工作分开记录。

复跑（app 目录；数据库 URL 通过环境安全注入，不写业务凭据）：

```bash
.venv/bin/ruff check .
.venv/bin/pyright
# 同时设置 DELIVERY_TEST_DATABASE_URL（仅 *_tests 测试库）
# 与 WEB_INTERACTION_CONSUMER_VECTORS（tpl-app 的共享向量绝对路径）
.venv/bin/pytest -q -rs
```

设计校准：[HTTPX SNI 扩展](https://www.python-httpx.org/advanced/extensions/)、
[OWASP SSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)。

## 回滚

无数据库迁移、无依赖变更；用反向提交撤销本包并重跑原套件。不自动部署回滚。
旧路径存在原 SSRF/大小检查缺口，因此不能把撤销安全修复当作恢复生产抓取的默认方案；
如现有来源不兼容，先停用/隔离该来源并明确受权策略。
