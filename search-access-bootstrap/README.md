# search-access-bootstrap

本目录是 Backend 的标准 Elasticsearch 接入能力，模板和实例化 App 默认保留
并启用。具体业务功能实现时，每类搜索数据集只能由一个 Backend 权威写入；
其他 Backend 通过 API、事件或任务消息使用该数据，不能绕过所有者直接写入。

声明不保存凭据；平台 Provisioner 创建索引、别名、角色、用户以及 Kubernetes
Secret/ConfigMap。
