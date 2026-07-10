# iBan Model Proxy

一个基于 `aiohttp` 的轻量级模型代理服务，用来将上游大模型接口统一暴露为 OpenAI 风格的 `/v1/chat/completions` 和 `/v1/embeddings` 接口，并借助 Redis 实现连接事件广播与调用统计。

项目当前特性：

- 兼容 OpenAI 风格的聊天补全与向量请求转发
- 支持普通响应和流式响应
- 使用 Redis 发布连接事件与实时统计
- 通过环境变量注入上游模型配置
- 提供 Dockerfile 和 Docker Compose 部署方式

## 架构说明

服务由两部分组成：

- `model_proxy`：对外提供 HTTP 接口，负责请求转发、流式透传、统计写入
- `model-redis`：负责事件广播、统计计数和 SSE 订阅支撑

核心流程如下：

1. 客户端请求 `POST /v1/chat/completions` 或 `POST /v1/embeddings`
2. `model_proxy` 读取请求体并记录简要请求信息
3. 请求被转发到上游模型服务
4. 调用结果写入 Redis 统计
5. 连接打开、关闭、实时统计通过 Redis Pub/Sub 广播
6. 前端或监控侧可通过 `GET /events` 订阅事件流

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

## 环境变量

程序启动时会读取以下环境变量：

| 变量名 | 必填 | 说明 |
| --- | --- | --- |
| `MODEL_API_KEY` | 是 | 上游模型服务的 API Key |
| `MODEL_BASE_URL` | 是 | 上游模型服务的基础地址，例如 `https://example.com` |
| `MODEL_NAME` | 是 | chat/completions 使用的模型名 |
| `EMBEDDING_API_KEY` | 是 | embeddings 上游服务的 API Key |
| `EMBEDDING_BASE_URL` | 是 | embeddings 上游服务的基础地址，例如 `https://example.com` |
| `EMBEDDING_NAME` | 是 | embeddings 使用的模型名 |
| `REDIS_URL` | 是 | Redis 连接地址，例如 `redis://model-redis:6379` |

推荐在项目根目录创建 `.env` 文件：

```env
MODEL_API_KEY=your_api_key
MODEL_BASE_URL=https://your-model-provider.example.com
MODEL_NAME=your_chat_model
EMBEDDING_API_KEY=your_embedding_api_key
EMBEDDING_BASE_URL=https://your-embedding-provider.example.com
EMBEDDING_NAME=your_embedding_model
REDIS_URL=redis://model-redis:6379
```

说明：

- 当前源码读取的是“进程环境变量”，不会自动解析 `.env` 文件
- `docker compose` 会通过 `env_file` 把 `.env` 注入容器，所以容器部署时可以直接使用 `.env`
- 如果你之前使用的是 `API_KEY` 或 `BASE_URL`，需要同步改成新的变量名
- `docker-compose.yml` 中已经为容器注入了 `REDIS_URL=redis://model-redis:6379`

## 接口说明

### `POST /v1/chat/completions`

用途：
将请求转发到上游模型接口 `${MODEL_BASE_URL}/v1/chat/completions`

请求头：

```http
Authorization: Bearer <由服务端注入，无需客户端传入>
Content-Type: application/json
```

请求体示例：

```json
{
  "model": "gpt-4o-mini",
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
- 服务端会强制覆盖请求体中的 `model` 为 `MODEL_NAME`
- 成功时累计 token 统计
- 失败时累计失败次数

### `POST /v1/embeddings`

用途：
将请求转发到上游模型接口 `${EMBEDDING_BASE_URL}/v1/embeddings`

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
- 服务端会强制覆盖请求体中的 `model` 为 `EMBEDDING_NAME`
- 成功时累计 token 统计
- 失败时累计失败次数

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

## Redis 中的频道与键

频道：

- `conn:events`
- `llm:stats:realtime`

统计键：

- `llm:stats:success`
- `llm:stats:fail`
- `llm:stats:tokens`
- `llm:stats:month`：当前统计所属月份，格式 `YYYY-MM`，跨月时会自动清空统计

## 本地运行

### 方式一：直接使用 Python 启动

建议 Python 版本：

- `Python 3.11+`

安装依赖：

```bash
pip install -r requirements.txt
```

如果你要直接在本机运行，并且配置写在 `.env` 中，先把变量导入当前 shell：

```bash
set -a
source .env
set +a
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
docker run --rm -p 8000:8000 --env-file .env iban-model-proxy:local
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

## 关于当前 Compose 配置的重要说明

当前 [docker-compose.yml](./docker-compose.yml) 中，`model_proxy` 使用的是远程镜像：

```yaml
image: origin-hub-ai-registry.cn-shanghai.cr.aliyuncs.com/dataflow/model_proxy:v1.0.0
```

这意味着：

- 你修改本地 `src/main.py` 后，`docker compose up` 不会自动用到本地最新代码
- 如果日志里仍然出现旧依赖或旧启动命令，通常说明容器跑的还是远程旧镜像

如果你希望 Compose 直接使用当前仓库代码，建议改成：

```yaml
model_proxy:
  build: .
```

然后执行：

```bash
docker compose up --build
```

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

- 修改代码后，如果你使用的是远程镜像方式，需要重新构建并发布镜像
- 如果你在本地频繁调试，建议把 Compose 改为 `build: .`
- 上游模型地址建议只填基础域名，不要重复拼接 `/v1/chat/completions` 或 `/v1/embeddings`

## 许可证

本项目采用 [LICENSE](./LICENSE) 中声明的许可证。
