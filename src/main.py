import asyncio
import os
import re
import time
import json
import logging
from http import HTTPStatus
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import aiohttp
import yaml
from redis.asyncio import Redis
from aiohttp import web, ClientSession, ClientTimeout
from src.stats import StatsRecorder, get_stats_snapshot, extract_usage, usage_counts
from src.key_registry import (
    AUTHORIZATION_MAPPING_KEY, PROJECT_MAPPING_KEY, normalize_authorization,
    authorization_stats_id, get_authorization_mappings, get_project_mappings,
    get_authorization_context, mutate_registry,
)

logging.basicConfig(level=logging.INFO)

BROADCAST_CHANNEL = "conn:events"
SUCCESS_COUNT_KEY = "llm:stats:success"
FAIL_COUNT_KEY = "llm:stats:fail"
TOTAL_TOKENS_KEY = "llm:stats:tokens"
STATS_MONTH_KEY = "llm:stats:month"
STATS_DIMENSION_KEYS = "llm:stats:dimension:keys"
STATS_BATCH_IDS = "llm:stats:batch_ids"
REALTIME_STATS_CHANNEL = "llm:stats:realtime"
PROXY_REQUEST_PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/embeddings", "/file_parse"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "model.yaml"
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(1024 * 1024 * 1024)))
DEFAULT_MINERU_BACKEND = "vlm-engine"


def normalize_model_style(style: str | None) -> str | None:
    style_map = {
        "openai": "chat",
        "chat": "chat",
        "completion": "chat",
        "completions": "chat",
        "embedding": "embedding",
        "embeddings": "embedding",
        "embdding": "embedding",
        "mineru": "mineru",
    }
    return style_map.get((style or "").strip().lower())


def load_model_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    if not isinstance(raw_config, dict):
        raise RuntimeError("model.yaml must be a mapping")

    redis_url = raw_config.get("redis_url", DEFAULT_REDIS_URL)
    raw_models = raw_config.get("models", raw_config)

    if not isinstance(raw_models, dict) or not raw_models:
        raise RuntimeError("model.yaml must define at least one model")

    models = {}
    for request_model, model_config in raw_models.items():
        if request_model == "redis_url":
            continue

        if not isinstance(model_config, dict):
            raise RuntimeError(f"Invalid config for model '{request_model}'")

        api_key = model_config.get("api_key")
        base_url = model_config.get("base_url")
        style = normalize_model_style(model_config.get("style"))
        upstream_model = model_config.get("upstream_model") or request_model
        extra_params = model_config.get("extra_params") or {}
        stream_include_usage = model_config.get("stream_include_usage", True)

        if not base_url:
            raise RuntimeError(f"Model '{request_model}' missing base_url")
        if not style:
            raise RuntimeError(
                f"Model '{request_model}' has invalid style '{model_config.get('style')}'"
            )
        if style != "mineru" and not api_key:
            raise RuntimeError(f"Model '{request_model}' missing api_key")
        if not isinstance(extra_params, dict):
            raise RuntimeError(f"Model '{request_model}' extra_params must be a mapping")
        if not isinstance(stream_include_usage, bool):
            raise RuntimeError(f"Model '{request_model}' stream_include_usage must be a boolean")

        models[request_model] = {
            "api_key": api_key,
            "base_url": str(base_url).rstrip("/"),
            "style": style,
            "upstream_model": upstream_model,
            "extra_params": dict(extra_params),
            "stream_include_usage": stream_include_usage,
            "backend": str(model_config.get("backend") or DEFAULT_MINERU_BACKEND),
        }

    return {
        "redis_url": redis_url,
        "models": models,
    }


MODEL_CONFIG = load_model_config()
REDIS_URL = MODEL_CONFIG["redis_url"]
MODEL_ROUTES = MODEL_CONFIG["models"]
MINERU_ROUTES = [route for route in MODEL_ROUTES.values() if route["style"] == "mineru"]

if len(MINERU_ROUTES) > 1:
    raise RuntimeError("model.yaml can define only one style: mineru route")

MINERU_ROUTE = MINERU_ROUTES[0] if MINERU_ROUTES else None


def get_expected_route_style(request_path: str) -> str:
    if request_path in {"/v1/chat/completions", "/v1/responses"}:
        return "chat"
    if request_path == "/v1/embeddings":
        return "embedding"
    raise ValueError(f"Unsupported request path: {request_path}")


def resolve_model_route(request_path: str, request_model: str | None) -> tuple[dict | None, str | None, int | None]:
    if not request_model:
        return None, "Missing required field: model", 400

    route = MODEL_ROUTES.get(request_model)
    if not route:
        return None, f"Unknown model: {request_model}", 404

    expected_style = get_expected_route_style(request_path)
    if route["style"] != expected_style:
        return None, f"Model '{request_model}' is not available for {request_path}", 400

    return route, None, None


def build_json_preview(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_response_preview(payload) -> str:
    """Extract display text from a Responses API payload when available."""
    if not isinstance(payload, dict):
        return build_json_preview(payload)

    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text

    text_parts = []
    for output in payload.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                text_parts.append(content["text"])
    return "".join(text_parts) or build_json_preview(payload)


def record_stats(request, success: bool, usage=None):
    if request.get("stats_recorded"):
        return
    dimensions = {}
    if authorization_id := request.get("stats_authorization_id"):
        dimensions["authorization"] = authorization_id
    if project_id := request.get("stats_project_id"):
        dimensions["project"] = project_id
    if model := request.get("stats_model"):
        dimensions["model"] = model
    total, input_tokens, output_tokens = usage_counts(usage)
    duration_ms = max(0, int((time.monotonic() - request["stats_start_ts"]) * 1000))
    request.app["stats"].record(success, total, dimensions,
                                input_tokens=input_tokens, output_tokens=output_tokens,
                                duration_ms=duration_ms)
    request["stats_recorded"] = True


async def publish_authorization_mappings(redis):
    await redis.publish(BROADCAST_CHANNEL, json.dumps({
        "type": "authorization_mappings_update",
        "mappings": await get_authorization_mappings(redis),
        "ts": int(time.time() * 1000),
    }, ensure_ascii=False))


async def publish_project_mappings(redis):
    await redis.publish(BROADCAST_CHANNEL, json.dumps({
        "type": "project_mappings_update",
        "mappings": await get_project_mappings(redis),
        "ts": int(time.time() * 1000),
    }, ensure_ascii=False))


def attach_proxy_error(
        request,
        upstream_path: str,
        error_message: str,
        *,
        stage: str,
        status: int,
):
    request["close_error"] = error_message
    request["close_error_stage"] = stage
    request["close_error_status"] = status
    request["close_upstream_path"] = upstream_path


def publish_broadcast_background(app, payload):
    task = asyncio.create_task(app["redis"].publish(
        BROADCAST_CHANNEL, json.dumps(payload, ensure_ascii=False)
    ))
    app["broadcast_tasks"].add(task)

    def completed(done):
        app["broadcast_tasks"].discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Connection event publish failed")

    task.add_done_callback(completed)


def request_remote(request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, (tuple, list)) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return request.remote or "-"


@web.middleware
async def request_log_middleware(request, handler):
    status = 500
    reason = HTTPStatus.INTERNAL_SERVER_ERROR.phrase
    try:
        response = await handler(request)
        status = response.status
        reason = response.reason or HTTPStatus(status).phrase
        return response
    except web.HTTPException as exc:
        status = exc.status
        reason = exc.reason or HTTPStatus(status).phrase
        raise
    finally:
        publish_broadcast_background(request.app, {
            "type": "request_log",
            "remote": request_remote(request),
            "method": request.method,
            "path": request.path_qs,
            "http_version": f"HTTP/{request.version.major}.{request.version.minor}",
            "status": status,
            "reason": reason,
            "ts": int(time.time() * 1000),
        })


@web.middleware
async def conn_broadcast_middleware(request, handler):
    if request.path not in PROXY_REQUEST_PATHS or request.method != "POST":
        return await handler(request)

    request["stats_start_ts"] = time.monotonic()
    conn_id = uuid4().hex
    request["conn_id"] = conn_id
    redis = request.app["redis"]
    authorization_description = None
    status = 500
    try:
        authorization_description, context = await get_authorization_context(
            redis, request.headers.get("Authorization"))
        if context:
            request["stats_authorization_id"] = authorization_stats_id(context["environment_authorization"])
            if context.get("project"):
                request["stats_project_id"] = authorization_stats_id(context["authorization"])

        if request.path == "/file_parse":
            proxy_req = {"service": "mineru", "backend": MINERU_ROUTE["backend"] if MINERU_ROUTE else "-"}
            if MINERU_ROUTE:
                request["stats_model"] = next(name for name, route in MODEL_ROUTES.items() if route is MINERU_ROUTE)
        else:
            try:
                proxy_req = await request.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise web.HTTPBadRequest(text="Invalid JSON body")
            if not isinstance(proxy_req, dict):
                raise web.HTTPBadRequest(text="JSON body must be an object")
            response_format = proxy_req.get("response_format")
            if isinstance(response_format, dict):
                schema = response_format.get("json_schema")
                if isinstance(schema, dict) and isinstance(schema.get("schema"), str):
                    try:
                        schema["schema"] = json.loads(schema["schema"])
                    except ValueError:
                        raise web.HTTPBadRequest(text="Invalid response_format schema")
        request["proxy_req"] = proxy_req

        await redis.publish(BROADCAST_CHANNEL, json.dumps({
            "type": "conn_open", "conn_id": conn_id, "remote": request.remote,
            "path": request.path, "authorization": authorization_description or "未配置",
            "request_info": proxy_req, "ts": int(time.time() * 1000),
        }, ensure_ascii=False))
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        attach_proxy_error(request, request.path, exc.text, stage="request", status=status)
        return web.json_response({"error": exc.text}, status=status)
    finally:
        if not request.get("stats_recorded"):
            record_stats(request, success=200 <= status < 300)
        publish_broadcast_background(request.app, {
            "type": "conn_close", "conn_id": conn_id, "remote": request.remote,
            "path": request.path, "authorization": authorization_description or "未配置",
            "status": request.get("close_error_status", status),
            "stage": request.get("close_error_stage"), "error": request.get("close_error"),
            "upstream_path": request.get("close_upstream_path"),
            "response_preview": request.get("response_preview"),
            "cost_ms": int((time.monotonic() - request["stats_start_ts"]) * 1000),
            "upstream_ms": request.get("upstream_ms"), "ts": int(time.time() * 1000),
        })


async def proxy_json_request(request, upstream_path: str):
    proxy_req = request.get("proxy_req")
    if not proxy_req:
        return web.json_response({"error": "Invalid request"}, status=400)

    route, route_error, route_status = resolve_model_route(
        request.path,
        proxy_req.get("model"),
    )
    if route_error:
        attach_proxy_error(
            request,
            upstream_path,
            route_error,
            stage="model_route",
            status=route_status,
        )
        record_stats(request, success=False)
        return web.json_response({"error": route_error}, status=route_status)

    request["stats_model"] = proxy_req["model"]
    upstream_req = dict(proxy_req)
    upstream_req["model"] = route["upstream_model"]
    if upstream_req.get("stream") and request.path == "/v1/chat/completions" and route["stream_include_usage"]:
        stream_options = upstream_req.get("stream_options")
        if stream_options is None:
            stream_options = {}
            upstream_req["stream_options"] = stream_options
        if isinstance(stream_options, dict):
            stream_options = dict(stream_options)
            upstream_req["stream_options"] = stream_options
            stream_options.setdefault("include_usage", True)
    if route["extra_params"]:
        upstream_req["extra_body"] = route["extra_params"]

    session = request.app["client_session"]
    upstream_start_ts = time.monotonic()
    try:
        async with session.post(
                f"{route['base_url']}{upstream_path}",
                headers={"Authorization": f"Bearer {route['api_key']}", "Content-Type": "application/json"},
                json=upstream_req,
                timeout=ClientTimeout(total=300)
        ) as resp:
            if resp.status != 200:
                error_message = f"LLM API Error: {await resp.text()}"
                attach_proxy_error(
                    request,
                    upstream_path,
                    error_message,
                    stage="upstream_response",
                    status=resp.status,
                )
                record_stats(request, success=False)
                return web.json_response(
                    {"error": error_message},
                    status=resp.status
                )

            if not upstream_req.get("stream"):
                try:
                    result = await resp.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    response_text = await resp.text()
                    error_message = f"Response parse error: {response_text or str(exc)}"
                    attach_proxy_error(
                        request,
                        upstream_path,
                        error_message,
                        stage="response_parse",
                        status=502,
                    )
                    record_stats(request, success=False)
                    return web.json_response({"error": error_message}, status=502)
                if request.path == "/v1/chat/completions":
                    request["response_preview"] = build_json_preview(result)
                elif request.path == "/v1/responses":
                    request["response_preview"] = get_response_preview(result)
                record_stats(request, success=True, usage=result)
                return web.json_response(result)

            # 透传上游流，并从事件中提取 usage。
            response = web.StreamResponse(
                status=resp.status,
                headers={"Content-Type": "text/event-stream; charset=utf-8"},
            )
            await response.prepare(request)

            last_usage = None
            stream_failed = False
            stream_preview_parts = []
            async for chunk in resp.content:
                chunk_str = chunk.decode("utf-8", errors="ignore")
                data = chunk_str.lstrip()
                if data.startswith("data:") and data[5:].strip() != "[DONE]":
                    try:
                        payload = json.loads(data[5:].strip())
                        if not isinstance(payload, dict):
                            payload = {}
                        if payload.get("error") or payload.get("type") in {"error", "response.failed", "response.incomplete"}:
                            stream_failed = True
                        if usage := extract_usage(payload):
                            last_usage = usage
                        if request.path == "/v1/chat/completions":
                            for choice in payload.get("choices", []):
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if isinstance(content, str):
                                    stream_preview_parts.append(content)
                        elif request.path == "/v1/responses" and payload.get("type") == "response.output_text.delta":
                            delta = payload.get("delta")
                            if isinstance(delta, str):
                                stream_preview_parts.append(delta)
                    except (json.JSONDecodeError, KeyError):
                        pass
                await response.write(chunk)

            if request.path in {"/v1/chat/completions", "/v1/responses"} and stream_preview_parts:
                request["response_preview"] = "".join(stream_preview_parts)
            await response.write_eof()
            if stream_failed:
                attach_proxy_error(request, upstream_path, "Upstream stream reported an error", stage="upstream_stream", status=502)
            record_stats(request, success=not stream_failed, usage=last_usage)
            return response

    except Exception as exc:
        attach_proxy_error(
            request,
            upstream_path,
            f"Internal error: {str(exc)}",
            stage="proxy_exception",
            status=500,
        )
        record_stats(request, success=False)
        return web.json_response({"error": "Internal error"}, status=500)
    finally:
        request["upstream_ms"] = int((time.monotonic() - upstream_start_ts) * 1000)


async def chat(request):
    return await proxy_json_request(request, "/v1/chat/completions")


async def responses(request):
    return await proxy_json_request(request, "/v1/responses")


async def embeddings(request):
    return await proxy_json_request(request, "/v1/embeddings")


async def build_mineru_form_data(request, backend: str) -> aiohttp.FormData:
    form_data = aiohttp.FormData()

    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        while part := await reader.next():
            if part.name == "backend":
                await part.release()
                continue
            if part.filename:
                field_options = {"filename": part.filename}
                if content_type := part.headers.get("Content-Type"):
                    field_options["content_type"] = content_type
                form_data.add_field(
                    part.name,
                    await part.read(),
                    **field_options,
                )
            else:
                form_data.add_field(part.name, await part.text())
    elif request.content_type == "application/x-www-form-urlencoded":
        fields = await request.post()
        for name, value in fields.items():
            if name != "backend":
                form_data.add_field(name, value)
    else:
        raise web.HTTPBadRequest(text="MinerU /file_parse expects form data")

    form_data.add_field("backend", backend)
    return form_data


async def mineru_file_parse(request):
    if MINERU_ROUTE is None:
        error_message = "MinerU is not configured"
        attach_proxy_error(request, "/file_parse", error_message, stage="mineru_route", status=503)
        record_stats(request, success=False)
        return web.json_response({"error": error_message}, status=503)

    try:
        form_data = await build_mineru_form_data(request, MINERU_ROUTE["backend"])
        headers = {}
        if accept := request.headers.get("Accept"):
            headers["Accept"] = accept

        async with request.app["client_session"].post(
            f"{MINERU_ROUTE['base_url']}/file_parse",
            headers=headers,
            data=form_data,
            timeout=ClientTimeout(total=300),
        ) as response:
            response_body = await response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            success = 200 <= response.status < 300
            if "json" in content_type.lower():
                try:
                    request["response_preview"] = build_json_preview(json.loads(response_body))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            if not success:
                attach_proxy_error(
                    request,
                    "/file_parse",
                    response_body.decode("utf-8", errors="replace")[:10_000],
                    stage="mineru_response",
                    status=response.status,
                )
            record_stats(request, success=success)
            return web.Response(status=response.status, body=response_body, headers={"Content-Type": content_type})
    except web.HTTPException as exc:
        attach_proxy_error(request, "/file_parse", exc.text, stage="mineru_request", status=exc.status)
        record_stats(request, success=False)
        return web.json_response({"error": exc.text}, status=exc.status)
    except Exception as exc:
        error_message = f"MinerU proxy error: {exc}"
        attach_proxy_error(request, "/file_parse", error_message, stage="mineru_proxy", status=502)
        record_stats(request, success=False)
        return web.json_response({"error": error_message}, status=502)


async def authorization_mappings(request):
    redis = request.app["redis"]

    if request.method == "GET":
        return web.json_response({"mappings": await get_authorization_mappings(redis)})

    try:
        payload = await request.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError):
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    authorization = payload.get("authorization") if isinstance(payload, dict) else None
    if not isinstance(authorization, str) or not authorization.strip():
        return web.json_response({"error": "authorization is required"}, status=400)
    authorization = normalize_authorization(authorization)
    if not authorization:
        return web.json_response({"error": "authorization is required"}, status=400)

    if request.method == "POST":
        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            return web.json_response({"error": "description is required"}, status=400)
        result = await mutate_registry(redis, "save_environment", authorization, description.strip())
    elif request.method == "DELETE":
        result = await mutate_registry(redis, "delete_environment", authorization)
    else:
        return web.json_response({"error": "Method not allowed"}, status=405)

    if result != "ok":
        message = {"project_key": "该密钥已用于项目，请使用另一个环境密钥", "has_projects": "请先删除该环境下的项目"}.get(result, "环境配置失败")
        return web.json_response({"error": message}, status=409)

    await publish_authorization_mappings(redis)
    return web.json_response({"mappings": await get_authorization_mappings(redis)})


async def project_mappings(request):
    redis = request.app["redis"]
    if request.method == "GET":
        return web.json_response({"mappings": await get_project_mappings(redis)})
    try:
        payload = await request.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError):
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    authorization = normalize_authorization(payload.get("authorization"))
    if not authorization:
        return web.json_response({"error": "authorization is required"}, status=400)
    if request.method == "POST":
        name = payload.get("name")
        environment = normalize_authorization(payload.get("environment_authorization"))
        if not isinstance(name, str) or not name.strip() or not environment:
            return web.json_response({"error": "name and environment are required"}, status=400)
        result = await mutate_registry(redis, "save_project", authorization, json.dumps({
            "name": name.strip(), "environment_authorization": environment,
        }, ensure_ascii=False), environment)
    elif request.method == "DELETE":
        result = await mutate_registry(redis, "delete_project", authorization)
    else:
        return web.json_response({"error": "Method not allowed"}, status=405)
    if result != "ok":
        message = {
            "missing_environment": "所属环境不存在，请先创建环境",
            "environment_key": "该密钥已用于环境，请使用另一个项目密钥",
            "different_environment": "项目密钥的所属环境不可更换，请为新环境创建新的项目密钥",
        }.get(result, "项目配置失败")
        return web.json_response({"error": message}, status=409)
    await publish_project_mappings(redis)
    return web.json_response({"mappings": await get_project_mappings(redis)})


async def events(request):
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream; charset=utf-8"}
    )
    await resp.prepare(request)

    pubsub = request.app["redis"].pubsub()
    await pubsub.subscribe(BROADCAST_CHANNEL, REALTIME_STATS_CHANNEL)

    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await resp.write(f"data: {msg['data']}\n\n".encode("utf-8"))
    except aiohttp.client_exceptions.ClientConnectionResetError:
        logging.warning("Browser disconnected")
    finally:
        await pubsub.unsubscribe(BROADCAST_CHANNEL, REALTIME_STATS_CHANNEL)
    return resp


async def stats(request):
    mappings = await get_authorization_mappings(request.app["redis"])
    environment_keys = {item["authorization"] for item in mappings}
    projects = await get_project_mappings(request.app["redis"], environment_keys)
    labels = {authorization_stats_id(item["authorization"]): item["description"] for item in mappings}
    project_labels = {authorization_stats_id(item["authorization"]): {
        "name": item["name"],
        "environment_id": authorization_stats_id(item["environment_authorization"]),
    } for item in projects}
    day = request.query.get("date")
    if day:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                raise ValueError
        except ValueError:
            return web.json_response({"error": "date must be YYYY-MM-DD"}, status=400)
    snapshot = await get_stats_snapshot(
        request.app["redis"],
        [STATS_MONTH_KEY, SUCCESS_COUNT_KEY, FAIL_COUNT_KEY, TOTAL_TOKENS_KEY],
        labels,
        project_labels,
        day,
        model_names=list(MODEL_ROUTES),
    )
    return web.json_response(snapshot, headers={"Cache-Control": "no-store"})


async def monitor(request):
    return web.FileResponse(STATIC_DIR / "monitor.html")


async def startup(app):
    app["redis"] = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    app["broadcast_tasks"] = set()
    app["client_session"] = ClientSession(timeout=ClientTimeout(total=300))
    app["stats"] = StatsRecorder(
        app["redis"],
        [STATS_MONTH_KEY, SUCCESS_COUNT_KEY, FAIL_COUNT_KEY, TOTAL_TOKENS_KEY,
         STATS_DIMENSION_KEYS, STATS_BATCH_IDS],
        REALTIME_STATS_CHANNEL,
    )
    app["stats"].start()


async def cleanup(app):
    await app["stats"].close()
    if app["broadcast_tasks"]:
        _, pending = await asyncio.wait(app["broadcast_tasks"], timeout=2)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await app["redis"].close()
    await app["client_session"].close()


app = web.Application(
    middlewares=[request_log_middleware, conn_broadcast_middleware],
    client_max_size=MAX_UPLOAD_SIZE,
)
app.router.add_post("/v1/chat/completions", chat)
app.router.add_post("/v1/responses", responses)
app.router.add_post("/v1/embeddings", embeddings)
app.router.add_post("/file_parse", mineru_file_parse)
app.router.add_get("/events", events)
app.router.add_get("/stats", stats)
app.router.add_get("/authorization-mappings", authorization_mappings)
app.router.add_post("/authorization-mappings", authorization_mappings)
app.router.add_delete("/authorization-mappings", authorization_mappings)
app.router.add_get("/project-mappings", project_mappings)
app.router.add_post("/project-mappings", project_mappings)
app.router.add_delete("/project-mappings", project_mappings)
app.router.add_get("/", monitor)
app.router.add_get("/monitor", monitor)

app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

if __name__ == "__main__":
    web.run_app(app, port=8080, access_log=logging.getLogger("aiohttp.access"))
