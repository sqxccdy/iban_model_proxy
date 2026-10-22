# iBan Model Proxy

一个基于 `aiohttp` 的轻量级模型代理服务，用来将上游大模型接口统一暴露为 OpenAI 风格的 `/v1/chat/completions`、`/v1/responses` 和 `/v1/embeddings` 接口，并借助 Redis 实现连接事件广播与调用统计。

项目当前特性：

- 兼容 OpenAI 风格的聊天补全与向量请求转发
- 支持普通响应和流式响应
- 使用 Redis 发布连接事件与实时统计
- 通过 `model.yaml` 配置多模型路由
- 提供 Dockerfile 和 Docker Compose 部署方式

## 架构说明

服务由两部分组成：

- `model_proxy`：对外提供 HTTP 接口，负责请求转发、流式透传、统计写入
- `model-redis`：负责事件广播、统计计数和 SSE 订阅支撑

核心流程如下：

1. 客户端请求 `POST /v1/chat/completions`、`POST /v1/responses` 或 `POST /v1/embeddings`
2. `model_proxy` 读取请求体并记录简要请求信息
3. 请求被转发到上游模型服务
4. 调用结果写入 Redis 统计
5. 连接打开、关闭、实时统计通过 Redis Pub/Sub 广播
6. 前端或监控侧可通过 `GET /events` 订阅事件流
7. 可通过 `GET /monitor` 或根路径 `/` 打开内置监控页面

## 目录结构

```text
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── src
    ├── __init__.py
    └── main.py
```

## 配置文件

程序启动时会读取项目根目录的 `model.yaml`。

你当前使用的简洁格式如下：

```yaml
text-embedding-3-small:
  api_key: your_embedding_api_key
  base_url: https://your-provider.example.com
  style: embdding

qwen3.5-plus:
  api_key: your_qwen_api_key
  base_url: https://your-provider.example.com
  style: openai

gpt-4o:
  api_key: your_gpt_api_key
  base_url: https://your-provider.example.com
  style: openai
```

字段说明：

- 顶层 key：客户端请求里传入的 `model`
- `api_key`：该模型对应上游服务的 API Key
- `base_url`：该模型对应上游服务基础地址
- `style`：模型类型
- `openai`：可用于 `POST /v1/chat/completions`
- `embedding` / `embeddings` / `embdding`：可用于 `POST /v1/embeddings`
- `mineru`：可用于 `POST /file_parse`，不需要 `model` 或 `api_key`

如果你希望“请求 model 名”和“实际转发给上游的 model 名”不一样，可以额外配置 `upstream_model`：

```yaml
gpt-4o:
  api_key: your_api_key
  base_url: https://your-provider.example.com
  style: openai
  upstream_model: gpt-4o-mini
```

也可以通过 `extra_params` 为指定模型固定注入额外请求字段。配置值会覆盖客户端请求中的同名字段：

```yaml
qwen3-32b:
  upstream_model: qwen3.5-plus
  api_key: your_api_key
  base_url: https://your-provider.example.com
  style: openai
  extra_params:
    enable_thinking: false
```

可选高级格式：

```yaml
redis_url: redis://model-redis:6379
models:
  gpt-4o:
    api_key: your_api_key
    base_url: https://your-provider.example.com
    style: openai
```

说明：

- 默认会从 `model.yaml` 中读取模型配置
- Redis 地址默认是 `redis://model-redis:6379`
- 如果设置了系统环境变量 `REDIS_URL`，会覆盖默认 Redis 地址
- 如果 `model.yaml` 使用了 `redis_url`，则优先使用该值

## 接口说明

### `POST /v1/chat/completions`

用途：
根据请求体中的 `model`，选择 `model.yaml` 里对应的 OpenAI 风格模型，并转发到对应上游 `${base_url}/v1/chat/completions`

请求头：

```http
Authorization: Bearer <由服务端注入，无需客户端传入>
Content-Type: application/json
```

请求体示例：

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Hello" }
  ],
  "stream": false
}
```

行为说明：

- 非流式请求：直接返回上游 JSON 响应
- 流式请求：以 `text/event-stream` 形式原样透传上游流
- 流式聊天请求默认向上游发送 `stream_options.include_usage: true`；若上游不返回 usage，该次 token 记为 0
- 如果上游不支持 `stream_options`，可在该模型配置中设置 `stream_include_usage: false`
- 服务端会按请求里的 `model` 选择路由配置
- 如果配置了 `upstream_model`，会把转发给上游的 `model` 改成该值
- 成功时累计 token；已配置的客户端 Authorization 还会单独累计用量
- 失败时累计失败次数

### `POST /v1/responses`

用途：
根据请求体中的 `model`，选择 `model.yaml` 中 `style: openai` 的模型，并转发到对应上游 `${base_url}/v1/responses`。

请求体示例：

```json
{
  "model": "gpt-4o",
  "input": "Hello",
  "stream": false
}
```

行为说明：

- 支持 OpenAI Responses API 的普通 JSON 响应和 SSE 流式响应透传
- 复用聊天模型的路由配置和 `upstream_model` 映射
- 成功与失败调用都会写入既有 Redis 统计；监控页面会展示可提取的输出文本
- 流式响应会从 `response.completed` 事件中的 `response.usage` 提取 token 用量

### `POST /v1/embeddings`

用途：
根据请求体中的 `model`，选择 `model.yaml` 里对应的 embedding 模型，并转发到对应上游 `${base_url}/v1/embeddings`

请求头：

```http
Authorization: Bearer <由服务端注入，无需客户端传入>
Content-Type: application/json
```

请求体示例：

```json
{
  "model": "text-embedding-3-small",
  "input": "hello world"
}
```

行为说明：

- 非流式 JSON 请求，直接返回上游响应
- 服务端会按请求里的 `model` 选择路由配置
- 如果配置了 `upstream_model`，会把转发给上游的 `model` 改成该值
- 成功时累计 token；已配置的客户端 Authorization 还会单独累计用量
- 失败时累计失败次数

### `POST /file_parse`

用途：
将 MinerU API 的文件解析请求透传到唯一的 `style: mineru` 配置。请求使用 `multipart/form-data`（或 URL 编码表单），文件和其它字段会原样保留；服务端会以配置中的 `backend` 覆盖客户端传入的同名字段。

配置示例：

```yaml
mineru:
  backend: vlm-engine
  base_url: https://192.168.1.201:8080
  style: mineru
```

MinerU 请求同样写入既有成功／失败统计与连接监控。配置多个 `style: mineru` 条目会阻止服务启动。

`/file_parse` 的上传上限由环境变量 `MAX_UPLOAD_SIZE` 控制，单位为字节，默认 `1073741824`（1 GiB）。

### `GET /events`

用途：
订阅 Redis 广播出来的连接事件和实时统计信息

返回类型：

```http
Content-Type: text/event-stream
```

事件内容包括：

- `conn_open`：连接打开事件
- `conn_close`：连接关闭事件
- `stats_update`：实时统计更新

补充字段：

- `conn_open` 和 `conn_close` 都会带 `conn_id`，便于前端准确跟踪当前活跃连接
- 当代理转发、模型路由或响应解析出错时，错误信息会直接挂在 `conn_close` 上
- 出错时 `conn_close` 会额外带 `stage`、`error` 和 `upstream_path`

### `GET /monitor`

用途：
打开内置静态监控页面。页面首次加载会从 `/stats` 读取本月累计值，随后订阅 `/events` 实时更新，并显示：

- 代理错误数
- 本月成功次数、失败次数、调用次数和累计 Token 数
- 本月环境密钥用量表，以及每日环境与项目、每日模型两张明细表
- 最近 50 条事件

在页面的密钥管理中保存 Authorization 和环境描述后，可用相同的 `Authorization: Bearer <密钥>` 发起代理请求。未配置的密钥不显示在项目明细中，但其请求仍计入月度总数和全部模型调用。

“每日环境与项目用量”保留环境、项目两级明细；选择环境或项目可查看该范围的六项指标。“每日模型用量”使用相同指标和表格列，可选择全部调用、某个环境或某个项目，查看范围内各模型的调用。两个日期输入同步，日期按北京时间、调用完成时间归档。

输入/输出 Token 从上游返回的 `usage.prompt_tokens` / `usage.completion_tokens`，或 `usage.input_tokens` / `usage.output_tokens` 提取；总用量优先取 `usage.total_tokens`，缺少时才合计输入与输出。上游未返回某项 usage 时，该项记录为 0。调用次数为成功加失败；成功率为成功次数除以调用次数；平均响应时长为完成调用的耗时总和除以调用次数，流式调用包含整段流完成时间。旧月度统计无法还原这些新增的历史每日字段。

匹配读取的是请求的 `Authorization` 请求头：`Bearer` 前缀大小写不敏感，前缀后的空格或制表符会被去掉；也支持直接发送裸密钥。其他认证方案（例如 `Token xxx`、`Basic xxx`）按完整请求头值匹配，配置时也要填完整值。`X-API-Key`、Cookie 等其他请求头不会参与此统计。

### `GET /stats`

返回当前月份的成功数、失败数、Token 总数及各维度明细。可用 `?date=YYYY-MM-DD` 读取指定日期；`daily.global`、`daily.environments`、`daily.projects`、`daily.models`、`daily.environment_models` 和 `daily.project_models` 包含每日六项指标所需的计数与派生字段，`daily.timezone` 为 `Asia/Shanghai`。项目和环境字段使用密钥的 SHA-256 标识，不返回原始密钥。这个接口只读取 Redis，不会为了测试而增加计数。

### `GET/POST/DELETE /project-mappings`

管理项目密钥。创建或更新时提交 JSON：`{"authorization":"项目密钥","name":"项目名称","environment_authorization":"环境密钥"}`；删除时只需提交 `authorization`。项目密钥与环境密钥都从请求的 `Authorization` 头识别，项目绑定的环境不能直接改为其他环境。

## Redis 中的频道与键

频道：

- `conn:events`
- `llm:stats:realtime`

统计键：

- `llm:stats:success`
- `llm:stats:fail`
- `llm:stats:tokens`
- `llm:stats:month`：当前统计所属月份，格式 `YYYY-MM`，跨月时会自动清空统计
- `llm:stats:dimension:authorization:success` / `fail` / `tokens`：Redis Hash，字段为已配置密钥的 SHA-256 标识
- `llm:stats:dimension:model:{success,fail,tokens}`：Redis Hash，字段为客户端请求的模型名
- `llm:stats:dimension:project_model:{success,fail,tokens,input_tokens,output_tokens,duration_ms}`：Redis Hash，字段为项目密钥标识与模型名的组合
- `llm:stats:dimension:environment_model:{success,fail,tokens,input_tokens,output_tokens,duration_ms}`：Redis Hash，字段为环境密钥标识与模型名的组合
- `llm:stats:daily:YYYY-MM-DD:{success,fail,tokens,input_tokens,output_tokens,duration_ms}`：每日 Redis Hash，字段包含 `global`、`authorization:<密钥标识>`、`project:<密钥标识>`、`model:<模型名>`、`environment_model:<环境标识>:<模型名>` 和 `project_model:<项目标识>:<模型名>`
- `monitor:project:mappings`、`monitor:project:owners`：项目配置及其环境归属
- `llm:stats:dimension:keys`：当前月份使用过的维度统计键集合，跨月时清空
- `llm:stats:batch_ids:YYYY-MM-DD`：当日已写入的批次标识，最长保留 72 小时，用于避免 Redis 重试时重复计数

统计在请求协程中只做内存聚合，由后台任务批量写入 Redis 并发布 `stats_update`。`/stats` 提供 `by_model`、`by_environment_model`、`by_project_model` 本月汇总以及对应每日指标。监控页保留整体月度汇总、本月环境表和每日环境→项目表，另加每日模型表；后者可按环境或项目查看各模型的输入、输出和总 Token。环境模型用量包含其项目调用。模型统计按请求中的模型名归类，MinerU 按配置的模型名归类；路由失败且无法识别模型的请求只计入全局。旧数据无法补出环境、项目与模型的交叉明细。

所有 `llm:stats:*` 统计键统一最长保留 72 小时，包括总量、模型/环境/项目聚合、每日明细与批次去重。写入使用 `EXPIRE ... NX`，后续请求不会延长已有键的期限；服务启动时清理过期每日旧键，并为其他旧统计键补齐/缩短 TTL，重启也不会延长既有期限。环境和项目密钥配置 `monitor:*` 不参与统计过期。

`/stats` 汇总是短期缓存值，到期后重新累计；`month` 仅表示当前月份，不能把缓存值当作完整月报。超过每日保留范围的 `/stats?date=...` 返回 HTTP 410。长期报表由 ADP 定时采集每日指标到 `ModelProxyStat`，前端查询 ADP 接口时只读该表。更改 `model.yaml` 不会立即删除统计中已有的模型名；旧模型名会随对应缓存到期消失。仍在运行的模型会继续产生新统计，所以服务有请求时 Redis 不会整体永久为空。
事件保留原有总数字段，并包含 `by_authorization`、`by_model`、`by_environment_model` 和 `by_project_model` 映射。以后增加统计维度时，可向统计记录器传入新的维度名和值；维度名会自动映射到对应的 Redis Hash。
Redis 暂时不可用时后台任务会重试；服务退出时最多等待 5 秒完成写入。如果上游没有提供 token usage，就只能记录该次请求的成功或失败，无法准确推算 token。

## 本地运行

### 方式一：直接使用 Python 启动

建议 Python 版本：

- `Python 3.11+`

安装依赖：

```bash
pip install -r requirements.txt
```

启动命令：

```bash
gunicorn -w 2 -k aiohttp.GunicornWebWorker -b 0.0.0.0:8000 src.main:app
```

如果只是临时调试，也可以直接运行：

```bash
python -m src.main
```

默认监听端口：

- `8000`：Gunicorn 启动时
- `8080`：直接执行 `python -m src.main` 时

## Docker 部署

### 构建镜像

```bash
docker build -t iban-model-proxy:local .
```

### 运行容器

```bash
docker run --rm -p 8000:8000 -v "$(pwd)/model.yaml:/app/model.yaml:ro" iban-model-proxy:local
```

## Docker Compose 部署

当前 `docker-compose.yml` 包含两个服务：

- `model-redis`
- `model_proxy`

其中：

- `model-redis` 配置了健康检查
- `model_proxy` 通过 `depends_on.condition: service_healthy` 等待 Redis 就绪后再启动
- `model_proxy` 对外暴露 `8000` 端口

启动：

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

## 查看当前代码的监控页面

已有 Redis 和有效的 `model.yaml` 时，按上面的“本地运行”方式启动当前代码，然后打开 `http://localhost:8080/monitor`（直接运行 Python）或 `http://localhost:8000/monitor`（Gunicorn）。`/stats` 可直接查看当前密钥统计的 JSON。

直接双击 `src/static/monitor.html` 无法访问代理接口。当前 [docker-compose.yml](./docker-compose.yml) 使用远程镜像 `model_proxy:v1.0.1`，不会自动包含本地代码修改；使用已有服务时，需确保实际运行的是更新后的代码。

## 常见问题

### 1. `duplicate base class TimeoutError`

原因：

- 使用了旧版 `aioredis`
- `aioredis` 与 Python 3.11/3.12 存在兼容性问题

处理方式：

- 使用 `redis` 官方库的异步接口
- 当前仓库依赖应为 `redis`，不要再使用 `aioredis`

### 2. `ModuleNotFoundError: No module named 'distutils'`

原因：

- 旧版 `aioredis` 在 Python 3.12+ 下仍依赖 `distutils`
- `distutils` 已从 Python 3.12 中移除

处理方式：

- 移除 `aioredis`
- 改用 `redis.asyncio`

### 3. `depends on undefined service "model-redis"`

原因：

- `model-redis` 被放进了某个未启用的 `profile`
- `model_proxy` 依赖了一个当前 Compose 视角下不存在的服务

处理方式：

- 不要把 `model-redis` 放到未启用的 profile 里
- 保持 `depends_on` 与服务定义在同一 compose 作用域下

### 4. `exec: "/usr/local/bin/gunicorn ...": no such file or directory`

原因：

- Dockerfile 中 `CMD` 把整条命令写成了一个字符串

正确写法：

```dockerfile
CMD ["gunicorn", "-w", "2", "-k", "aiohttp.GunicornWebWorker", "-b", "0.0.0.0:8000", "src.main:app"]
```

### 5. Redis 已经启动，但服务仍不可用

先确认是哪一类问题：

- 如果是导入阶段崩溃，通常是 Python 依赖兼容问题，不是 Redis 本身
- 如果是启动后连接失败，再检查 `REDIS_URL` 是否正确
- 容器场景下请优先使用 `redis://model-redis:6379`

## 开发建议

- 修改代码后需重启运行当前代码的代理进程，浏览器刷新 `/monitor` 查看新页面
- 上游模型地址建议只填基础域名，不要重复拼接 `/v1/chat/completions` 或 `/v1/embeddings`

## 许可证

本项目采用 [LICENSE](./LICENSE) 中声明的许可证。
