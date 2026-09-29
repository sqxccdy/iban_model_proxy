# model_proxy 按 Authorization 密钥统计说明

更新时间：2026-09-28。原项目：`/Users/haz/Desktop/projects/model_proxy`；修改项目：`/Users/haz/Desktop/projects/sort_model_proxy`。

桌面上此前以“按模型统计”为题的两份文档及源码截图记录的是旧方案；当前实现请以本文和项目 `README.md` 为准。

## 当前结果

修改项目继续保留原有的本月成功数、失败数、token 总数，并按**已在监控页配置的客户端 Authorization 密钥**分别统计成功、失败和 token。统计表只显示密钥配置的环境描述，不在统计接口或统计事件中发送原始密钥。未配置的密钥仍计入全局总数，但不生成密钥明细。模型只用于选择上游路由，不再作为统计维度。

统计写入在请求协程中只更新内存，后台任务再批量写 Redis。密钥描述查询和请求开始事件仍在请求协程中等待 Redis；请求结束事件改为后台发布。整个代理尚未做到 Redis 完全不可用时仍能无阻塞运行。

## 一次请求的完整过程

1. 在 `/monitor` 的密钥管理中保存密钥和环境描述。可以填裸密钥，也可以填 `Bearer <密钥>`；服务端统一保存裸密钥。此前保存的 `Bearer <密钥>` 映射也能继续识别。
2. 客户端发送 `Authorization: Bearer <密钥>`。中间件从 Redis 映射查询该密钥；只有配置过且描述非空时，才给这次请求附上该密钥的 SHA-256 标识。
3. 代理按请求体 `model` 路由至上游。请求体的模型名及配置的 `upstream_model` 均不影响密钥统计归属。
4. 普通 JSON 响应读取 `usage.total_tokens`。Responses 格式若把 usage 放在 `response.usage.total_tokens`，也会读取。流式请求逐行解析上游 SSE 数据，读取最后一个完整 usage 的 `total_tokens`。Chat 流式默认向上游发送 `stream_options.include_usage: true`；不兼容的上游可在该模型配置中设 `stream_include_usage: false`。
5. 请求结束时调用 `record_stats()`，同步操作只有内存计数和唤醒后台任务。后台 `StatsRecorder` 聚合一批计数后，用 Redis Lua 一次更新本月全局键、密钥维度 Hash 和实时事件。批次 ID 用于重试去重。
6. `/stats` 读取本月快照，`/events` 继续推送实时变化。监控页先获取快照，再通过 SSE 更新；密钥配置变化后会重新读取快照和描述。

示例：若密钥 `94365187-a741-457a-a7c2-09806b1b8025` 配置为“测试环境”，客户端以 `Authorization: Bearer 94365187-a741-457a-a7c2-09806b1b8025` 调用，并且上游返回 `usage.total_tokens = 37`，则全局 token 和“测试环境”的 token 各增加 37。这是同一用量的总计与分组视图，不是加起来 74。

## 修改位置

| 文件 | 修改内容 |
| --- | --- |
| `src/main.py` | 规范化 Authorization；查配置并生成哈希标识；在 Chat、Responses、Embeddings、MinerU 的结果路径记录统计；增加 `/stats`；启动和关闭统计后台任务。 |
| `src/stats.py` | 通用维度字典、内存聚合、后台 Redis Lua 写入、跨月重置、批次去重和统计快照。增加其他维度时，可向 `record()` 传入其他维度名和值。 |
| `src/static/monitor.html` | 本月密钥用量表、统计快照、SSE 更新及密钥映射刷新。 |
| `README.md` | 使用步骤、返回字段、Redis 键及部署注意事项。 |
| `tests/` | 异步聚合、密钥匹配、快照过滤和页面渲染验证。 |

Redis 全局键继续使用 `llm:stats:success`、`llm:stats:fail`、`llm:stats:tokens`、`llm:stats:month`。密钥明细使用 `llm:stats:dimension:authorization:{success,fail,tokens}` 三个 Hash，字段为密钥 SHA-256 标识。`/stats` 返回 `by_authorization` 计数和 `authorizations` 描述映射。

## 页面验证

1. 用当前项目代码启动代理与 Redis，打开 `http://localhost:8080/monitor`。如果通过 Gunicorn 端口 8000 运行，改用 `http://localhost:8000/monitor`。
2. 在密钥管理中新增密钥和“测试环境”描述。用该密钥发起一次普通 Chat 或 Embeddings 请求；在“本月密钥用量”查看成功/失败和 token。没有上游 usage 时，token 为 0，但成功或失败次数仍更新。
3. 打开 `/stats` 查看原始 JSON；这里的密钥标识是哈希值。未配置密钥发起的请求只会使页面上方总数增加。

当前 `docker-compose.yml` 的代理服务引用远程 `model_proxy:v1.0.1` 镜像，启动它不会自动加载这份本地修改。页面验证应运行当前源码或部署包含当前代码的新镜像。

## 已验证及限制

`python -m unittest discover -s tests -v` 验证了不等待 Redis 的内存记录、批次聚合、密钥匹配和快照过滤；`node tests/test_monitor.js` 验证了初始快照、实时计数和密钥描述变化后的刷新；`git diff --check` 通过。本轮没有连接真实上游，也没有完成本轮真实 Redis 集成测试：本机沙箱拒绝 Redis 创建测试用 Unix socket。先前按模型版本的 Redis 集成结果不能代替当前密钥版本的端到端验收。

上游不返回 usage、流在最终 usage 事件前中断时，无法准确取得 token，记为 0。后台缓冲在进程突然退出时可能丢失尚未写出的数据；Redis 不可用时后台会重试。要进一步提高 Redis 故障时的请求可用性，还需处理请求开始事件和描述查询的直接 Redis 等待。监控页显示请求耗时与上游耗时，便于定位响应慢的环节。`Authorization: Bearer <密钥>` 和直接发送裸密钥都能匹配；`Token xxx` 等其他方案需在配置中填写完整的 Authorization 值。其他请求头不参与此统计。
