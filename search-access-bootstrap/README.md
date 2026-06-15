# search-access-bootstrap

本目录是 Backend 的标准 Elasticsearch 接入能力，默认保留并启用。具体业务
功能实现时，每类搜索数据集只能由一个 Backend 权威写入；其他 Backend 通过
API、事件或任务消息使用该数据，不能绕过所有者直接写入。

该目录声明并调用平台 Elasticsearch Provisioner，创建索引模板、版本化索引、
读写别名、最小权限角色和运行凭据。

```bash
./search-access-bootstrap.sh validate
./search-access-bootstrap.sh provision
./search-access-bootstrap.sh status
./search-access-bootstrap.sh rotate
./search-access-bootstrap.sh revoke
```
