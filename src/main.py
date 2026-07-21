import os
import time
import json
import logging
from pathlib import Path
from uuid import uuid4

import aiohttp
import yaml
from redis.asyncio import Redis
from redis.exceptions import WatchError
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO)

BROADCAST_CHANNEL = "conn:events"
SUCCESS_COUNT_KEY = "llm:stats:success"
FAIL_COUNT_KEY = "llm:stats:fail"
TOTAL_TOKENS_KEY = "llm:stats:tokens"
STATS_MONTH_KEY = "llm:stats:month"
REALTIME_STATS_CHANNEL = "llm:stats:realtime"
PROXY_REQUEST_PATHS = {"/v1/chat/completions", "/v1/embeddings"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "model.yaml"
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


def normalize_model_style(style: str | None) -> str | None:
    style_map = {
        "openai": "chat",
        "chat": "chat",
        "completion": "chat",
        "completions": "chat",
        "embedding": "embedding",
        "embeddings": "embedding",
        "embdding": "embedding",
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

        if not api_key:
            raise RuntimeError(f"Model '{request_model}' missing api_key")
        if not base_url:
            raise RuntimeError(f"Model '{request_model}' missing base_url")
        if not style:
            raise RuntimeError(
                f"Model '{request_model}' has invalid style '{model_config.get('style')}'"
            )

        models[request_model] = {
            "api_key": api_key,
            "base_url": str(base_url).rstrip("/"),
            "style": style,
            "upstream_model": upstream_model,
        }

    return {
        "redis_url": redis_url,
        "models": models,
    }


MODEL_CONFIG = load_model_config()
REDIS_URL = MODEL_CONFIG["redis_url"]
MODEL_ROUTES = MODEL_CONFIG["models"]


def get_expected_route_style(request_path: str) -> str:
    if request_path == "/v1/chat/completions":
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


def get_current_stats_month() -> str:
    return time.strftime("%Y-%m", time.localtime())


async def ensure_monthly_stats(redis):
    current_month = get_current_stats_month()

    while True:
        try:
            async with redis.pipeline() as pipe:
                await pipe.watch(STATS_MONTH_KEY)
                stats_month = await pipe.get(STATS_MONTH_KEY)

                if stats_month == current_month:
                    await pipe.reset()
                    return

                pipe.multi()
                pipe.set(STATS_MONTH_KEY, current_month)
                pipe.set(SUCCESS_COUNT_KEY, 0)
                pipe.set(FAIL_COUNT_KEY, 0)
                pipe.set(TOTAL_TOKENS_KEY, 0)
                await pipe.execute()
                return
        except WatchError:
            continue


async def update_stats(redis, success: bool, tokens: int = 0):
    await ensure_monthly_stats(redis)

    pipe = redis.pipeline()
    pipe.incr(SUCCESS_COUNT_KEY) if success else pipe.incr(FAIL_COUNT_KEY)
    if tokens > 0:
        pipe.incrby(TOTAL_TOKENS_KEY, tokens)
    else:
        pipe.incrby(TOTAL_TOKENS_KEY, 0)
    pipe.get(SUCCESS_COUNT_KEY)
    pipe.get(FAIL_COUNT_KEY)
    pipe.get(TOTAL_TOKENS_KEY)

    _, _, s_cnt, f_cnt, t_tokens = await pipe.execute()
    await redis.publish(REALTIME_STATS_CHANNEL, json.dumps({
        "type": "stats_update",
        "success_count": int(s_cnt or 0),
        "fail_count": int(f_cnt or 0),
        "total_tokens": int(t_tokens or 0),
        "ts": int(time.time() * 1000)
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


@web.middleware
async def conn_broadcast_middleware(request, handler):
    start_ts = time.time()
    conn_id = uuid4().hex
    request["conn_id"] = conn_id
    redis = request.app["redis"]
    proxy_req = None

    # 只处理代理接口的请求，提前缓存请求体并强制模型
    if request.path in PROXY_REQUEST_PATHS and request.method == "POST":
        proxy_req = await request.json()
        request["proxy_req"] = proxy_req
    if request["proxy_req"]['model'] in [
        'qwen3.5-plus',
        'gpt-4o'
    ]:
        request["proxy_req"]['enable_thinking'] = False

    await redis.publish(BROADCAST_CHANNEL, json.dumps({
        "type": "conn_open",
        "conn_id": conn_id,
        "remote": request.remote,
        "path": request.path,
        "request_info": proxy_req,
        "ts": int(start_ts * 1000)
    }, ensure_ascii=False))

    status = 500
    try:
        resp = await handler(request)
        status = resp.status
    except Exception:
        raise
    finally:
        await redis.publish(BROADCAST_CHANNEL, json.dumps({
            "type": "conn_close",
            "conn_id": conn_id,
            "remote": request.remote,
            "path": request.path,
            "status": request.get("close_error_status", status),
            "stage": request.get("close_error_stage"),
            "error": request.get("close_error"),
            "upstream_path": request.get("close_upstream_path"),
            "response_preview": request.get("response_preview"),
            "cost_ms": int((time.time() - start_ts) * 1000),
            "ts": int(time.time() * 1000)
        }, ensure_ascii=False))
    return resp


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
        await update_stats(request.app["redis"], success=False)
        return web.json_response({"error": route_error}, status=route_status)

    upstream_req = dict(proxy_req)
    upstream_req["model"] = route["upstream_model"]

    session = request.app["client_session"]
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
                await update_stats(request.app["redis"], success=False)
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
                    await update_stats(request.app["redis"], success=False)
                    return web.json_response({"error": error_message}, status=502)
                if request.path == "/v1/chat/completions":
                    request["response_preview"] = build_json_preview(result)
                await update_stats(
                    request.app["redis"],
                    success=True,
                    tokens=result.get("usage", {}).get("total_tokens", 0)
                )
                return web.json_response(result)

            # 流式逻辑完全不变
            response = web.StreamResponse(
                status=resp.status,
                headers={"Content-Type": "text/event-stream; charset=utf-8"},
            )
            await response.prepare(request)

            last_usage = None
            stream_preview_parts = []
            async for chunk in resp.content:
                chunk_str = chunk.decode("utf-8", errors="ignore")
                if "data: " in chunk_str and "[DONE]" not in chunk_str:
                    try:
                        payload = json.loads(chunk_str.split("data: ")[-1].strip())
                        if "usage" in payload:
                            last_usage = payload["usage"]
                        if request.path == "/v1/chat/completions":
                            for choice in payload.get("choices", []):
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if isinstance(content, str):
                                    stream_preview_parts.append(content)
                    except (json.JSONDecodeError, KeyError):
                        pass
                await response.write(chunk)

            if request.path == "/v1/chat/completions" and stream_preview_parts:
                request["response_preview"] = "".join(stream_preview_parts)
            await update_stats(
                request.app["redis"],
                success=True,
                tokens=last_usage.get("total_tokens", 0) if last_usage else 0
            )
            await response.write_eof()
            return response

    except Exception as exc:
        attach_proxy_error(
            request,
            upstream_path,
            f"Internal error: {str(exc)}",
            stage="proxy_exception",
            status=500,
        )
        await update_stats(request.app["redis"], success=False)
        return web.json_response({"error": "Internal error"}, status=500)


async def chat(request):
    return await proxy_json_request(request, "/v1/chat/completions")


async def embeddings(request):
    return await proxy_json_request(request, "/v1/embeddings")


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


async def monitor(request):
    return web.FileResponse(STATIC_DIR / "monitor.html")


async def startup(app):
    app["redis"] = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    app["client_session"] = ClientSession(timeout=ClientTimeout(total=300))


async def cleanup(app):
    await app["redis"].close()
    await app["client_session"].close()


app = web.Application(middlewares=[conn_broadcast_middleware])
app.router.add_post("/v1/chat/completions", chat)
app.router.add_post("/v1/embeddings", embeddings)
app.router.add_get("/events", events)
app.router.add_get("/", monitor)
app.router.add_get("/monitor", monitor)

app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

if __name__ == "__main__":
    web.run_app(app, port=8080, access_log=logging.getLogger("aiohttp.access"))
