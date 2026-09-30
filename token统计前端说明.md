## 一、数据、接口与代码位置

| 文件或接口                                | 作用                                                      |
| ----------------------------------------- | --------------------------------------------------------- |
| `src/main.py`                             | 路由、中间件、模型转发、连接事件、统计入口、管理接口。    |
| `src/stats.py`                            | Token 提取、每日和月度指标、后台批量写 Redis、统计快照。  |
| `src/key_registry.py`                     | 环境和项目密钥配置、密钥匹配、环境归属及 Redis 原子更新。 |
| `src/static/monitor.html`                 | 监控页面、密钥管理和每日六项指标展示。                    |
| `model.yaml`                              | 模型路由、上游密钥和可选 Redis 地址。                     |
| `GET /stats`                              | 查询本月总数与每日明细；可带 `date=YYYY-MM-DD`。          |
| `GET/POST/DELETE /authorization-mappings` | 查询、保存、删除环境密钥。                                |
| `GET/POST/DELETE /project-mappings`       | 查询、保存、删除项目密钥。                                |
| `GET /events`                             | 连接及统计事件的 SSE 订阅。                               |

环境和项目名称、原始密钥及归属保存在 Redis；统计明细使用密钥的 SHA-256 标识作为字段，页面的统计表只显示环境和项目名称。原始密钥仍可在密钥管理列表及管理接口中看到。数据是否在 Redis 重启后保留，取决于实际 Redis 的持久化配置；项目代码本身没有为 Redis 强制开启 AOF。旧月度总量也无法反推出新增每日输入、输出和耗时明细。

当前 `docker-compose.yml` 的代理服务引用远端旧版 `model_proxy:v1.0.1` 镜像，它不会自动包含这个目录里的新功能。要查看当前修改，应运行本地源码或另行构建并部署当前源码镜像。

内置监控页显示整体月度调用汇总与原有本月环境表，下面保留每日环境→项目两级表，并新增一张使用相同六项指标和表格列的每日模型表。模型表可选择全部调用、环境或项目；选环境时读取 `daily.environment_models[envId]`，选项目时读取 `daily.project_models[projectId]`。

## 二、给新前端的接口与字段

以下路径以代理服务地址为基准。本地直接运行通常是 `http://localhost:8080`，Gunicorn 方式通常是 `http://localhost:8000`。当前后端没有额外配置跨域响应头；新页面最好与代理服务同源，或由前端服务器反向代理这些路径。所有管理接口目前也没有独立的管理员鉴权，且会返回原始密钥，部署时需要限制谁能访问。

### 1. 读取统计：`GET /stats?date=YYYY-MM-DD`

`date` 可省略，省略时取北京时间当天。日期格式必须是有效的 `YYYY-MM-DD`；非法日期返回 HTTP 400：`{"error":"date must be YYYY-MM-DD"}`。响应不缓存。下例中的 `envId`、`projectId` 是示意，实际为密钥 SHA-256 得到的 64 位十六进制标识，不能拿它代替调用密钥。

```json
{
  "type": "stats_update",
  "month": "2026-09",
  "success_count": 3,
  "fail_count": 1,
  "total_tokens": 24,
  "by_authorization": {
    "envId": {"success": 3, "fail": 0, "tokens": 24}
  },
  "by_model": {
    "gpt-4o": {"success": 3, "fail": 1, "tokens": 24}
  },
  "by_environment_model": {
    "envId": {
      "gpt-4o": {"success": 3, "fail": 0, "tokens": 24,
        "input_tokens": 16, "output_tokens": 8, "duration_ms": 900,
        "calls": 3, "success_rate": 100.0, "avg_duration_ms": 300.0}
    }
  },
  "by_project_model": {
    "projectId": {
      "gpt-4o": {"success": 2, "fail": 0, "tokens": 17,
        "input_tokens": 11, "output_tokens": 6, "duration_ms": 600,
        "calls": 2, "success_rate": 100.0, "avg_duration_ms": 300.0}
    }
  },
  "authorizations": {"envId": "测试环境"},
  "projects": {
    "projectId": {"name": "项目 A", "environment_id": "envId"}
  },
  "daily": {
    "date": "2026-09-29",
    "timezone": "Asia/Shanghai",
    "global": {
      "success": 3, "fail": 1, "tokens": 24,
      "input_tokens": 16, "output_tokens": 8, "duration_ms": 1000,
      "calls": 4, "success_rate": 75.0, "avg_duration_ms": 250.0
    },
    "environments": {
      "envId": {
        "success": 3, "fail": 0, "tokens": 24,
        "input_tokens": 16, "output_tokens": 8, "duration_ms": 900,
        "calls": 3, "success_rate": 100.0, "avg_duration_ms": 300.0
      }
    },
    "projects": {
      "projectId": {
        "success": 2, "fail": 0, "tokens": 17,
        "input_tokens": 11, "output_tokens": 6, "duration_ms": 600,
        "calls": 2, "success_rate": 100.0, "avg_duration_ms": 300.0
      }
    },
    "models": {
      "gpt-4o": {
        "success": 3, "fail": 1, "tokens": 24,
        "input_tokens": 16, "output_tokens": 8, "duration_ms": 1000,
        "calls": 4, "success_rate": 75.0, "avg_duration_ms": 250.0
      }
    },
    "environment_models": {
      "envId": {
        "gpt-4o": {
          "success": 3, "fail": 0, "tokens": 24,
          "input_tokens": 16, "output_tokens": 8, "duration_ms": 900,
          "calls": 3, "success_rate": 100.0, "avg_duration_ms": 300.0
        }
      }
    },
    "project_models": {
      "projectId": {
        "gpt-4o": {
          "success": 2, "fail": 0, "tokens": 17,
          "input_tokens": 11, "output_tokens": 6, "duration_ms": 600,
          "calls": 2, "success_rate": 100.0, "avg_duration_ms": 300.0
        }
      }
    }
  },
  "ts": 1790000000000
}
```

示例数值只用来展示结构；`ts` 是服务端生成响应时的 Unix 毫秒时间戳。各字段含义：

| 字段                                          | 类型与含义                                                   |
| --------------------------------------------- | ------------------------------------------------------------ |
| `month`                                       | 字符串，当前月度累计所属月份 `YYYY-MM`；即使查询过去日期，月度字段仍是当前月份。 |
| `success_count`、`fail_count`、`total_tokens` | 整数，全局本月累计。                                         |
| `by_authorization[envId]`                     | 当前已配置环境的本月 `success`、`fail`、`tokens`；键是环境密钥标识。 |
| `by_model[model]`                              | 按客户端请求模型名统计的本月 `success`、`fail`、`tokens`；已配置但未调用的模型也返回 0。 |
| `by_project_model[projectId][model]`           | 当前已配置项目中各模型的本月指标，包含输入、输出与总 Token。 |
| `by_environment_model[envId][model]`           | 当前已配置环境中各模型的本月指标，包含所属项目调用。 |
| `authorizations[envId]`                       | 环境密钥标识到环境名称的映射，用于显示和选项。               |
| `projects[projectId]`                         | 项目密钥标识到 `{name, environment_id}` 的映射；`environment_id` 对应 `authorizations` 的键。 |
| `daily.date`、`daily.timezone`                | 每日数据的日期和时区。                                       |
| `daily.global`                                | 所选日期所有调用的指标，包括未登记密钥。                     |
| `daily.environments[envId]`                   | 所选日期该环境直接调用与其项目调用的合计。                   |
| `daily.projects[projectId]`                   | 所选日期该项目密钥的调用。                                   |
| `daily.models[model]`                          | 所选日期该模型的调用，包含所有密钥。模型名使用请求值，而非 `upstream_model`。 |
| `daily.project_models[projectId][model]`       | 所选日期该项目调用该模型的指标。 |
| `daily.environment_models[envId][model]`       | 所选日期该环境直接调用及其项目调用该模型的合计。 |

`daily.global`、每个 `daily.environments[envId]`、`daily.projects[projectId]`、`daily.models[model]`、`daily.environment_models[envId][model]`、`daily.project_models[projectId][model]` 及对应的月度交叉指标对象结构相同：

| 字段              | 类型与展示建议                                               |
| ----------------- | ------------------------------------------------------------ |
| `tokens`          | 整数，Token 总用量。                                         |
| `input_tokens`    | 整数，输入 Token。                                           |
| `output_tokens`   | 整数，输出 Token。                                           |
| `calls`           | 整数，调用次数，等于 `success + fail`。                      |
| `success_rate`    | 数字或 `null`，百分数 0～100；`75` 应显示为 `75%`，不能再乘 100。没有调用时为 `null`，可显示 `—`。 |
| `avg_duration_ms` | 数字或 `null`，平均响应时长，单位毫秒；没有调用时为 `null`。 |
| `success`、`fail` | 整数，成功和失败次数，可用于明细表。                         |
| `duration_ms`     | 整数，所有调用的耗时总和，单位毫秒；一般不直接当平均时长展示。 |

环境、项目及其模型明细只返回当前仍配置的环境或项目；没有请求的已配置对象也会有值为 0 的指标对象。删除配置后，历史统计仍在 Redis，但不会出现在对应环境或项目的模型明细中。全局值不能通过把环境行相加获得，因为可能有未配置密钥；环境行已经包含其项目调用，也不能与项目行重复相加。后台统计写入是异步的，刚完成的调用可能要稍后才出现在查询结果中。旧数据无法补出环境、项目与模型的交叉明细。

### 2. 环境密钥管理：`/authorization-mappings`

| 方法     | JSON 请求体                                             | 成功响应                                                     |
| -------- | ------------------------------------------------------- | ------------------------------------------------------------ |
| `GET`    | 无                                                      | `{"mappings":[{"authorization":"环境密钥","description":"测试环境"}]}` |
| `POST`   | `{"authorization":"环境密钥","description":"测试环境"}` | 同 GET；相同密钥会更新名称。                                 |
| `DELETE` | `{"authorization":"环境密钥"}`                          | 同 GET；存在所属项目时返回 HTTP 409。                        |

`authorization` 是供请求端使用的真实密钥，前端密钥管理页可用；普通统计页应使用 `/stats.authorizations` 里的名称和标识。保存时传裸密钥或 `Bearer <密钥>` 都会规范化为裸密钥。环境密钥不能与项目密钥相同。

### 3. 项目密钥管理：`/project-mappings`

| 方法     | JSON 请求体                                                  | 成功响应                                                     |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `GET`    | 无                                                           | `{"mappings":[{"authorization":"项目密钥","name":"项目 A","environment_authorization":"环境密钥"}]}` |
| `POST`   | `{"authorization":"项目密钥","name":"项目 A","environment_authorization":"环境密钥"}` | 同 GET；相同密钥会更新项目名称。                             |
| `DELETE` | `{"authorization":"项目密钥"}`                               | 同 GET。                                                     |

先用环境接口创建环境，再创建项目。`environment_authorization` 要传环境的真实密钥，不能传 `/stats` 中的 `environment_id`。项目密钥一旦绑定环境，不能通过编辑改到另一个环境；新环境需要新项目密钥。原始项目密钥用于发起代理调用：`Authorization: Bearer <项目密钥>`。

管理接口使用 `Content-Type: application/json`。请求体缺少字段或格式错误通常返回 HTTP 400，关系冲突返回 HTTP 409，响应格式为 `{"error":"错误说明"}`。保存或删除成功后返回完整的最新 `mappings`，前端可直接刷新列表和环境下拉选项。

### 4. 实时事件：`GET /events`

响应是 `text/event-stream`，每条消息为 `data: <JSON>\n\n`，浏览器可用 `new EventSource('/events')` 并在 `onmessage` 中 `JSON.parse(event.data)`。`type` 决定消息结构：

| `type`                          | 主要字段                                                     | 前端用途                                                     |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `stats_update`                  | `month`、`success_count`、`fail_count`、`total_tokens`、`by_authorization`、`by_model`、`by_environment_model`、`by_project_model`、`ts` | 更新本月累计；**不含 `daily`、名称映射**，收到后重新请求 `/stats?date=...` 才能刷新每日六项指标。 |
| `authorization_mappings_update` | `mappings`、`ts`                                             | 环境配置变化，更新密钥管理列表并重新请求 `/stats`。          |
| `project_mappings_update`       | `mappings`、`ts`                                             | 项目配置变化，更新密钥管理列表并重新请求 `/stats`。          |
| `conn_open`                     | `conn_id`、`remote`、`path`、`authorization`、`request_info`、`ts` | 连接开始；`authorization` 是环境或项目的显示名称，未匹配时是“未配置”。 |
| `conn_close`                    | `conn_id`、`remote`、`path`、`authorization`、`status`、`stage`、`error`、`upstream_path`、`response_preview`、`cost_ms`、`upstream_ms`、`ts` | 连接结束；`conn_id` 与开始事件配对。`cost_ms` 是代理侧整次请求耗时。 |

`conn_close` 的 `stage`、`error`、`upstream_path`、`response_preview`、`upstream_ms` 在部分情况可能为 `null`。`request_info` 和 `response_preview` 可能包含调用内容，新前端若显示连接详情，需要按可访问范围处理。
