import os
import time
import json
import logging
from pathlib import Path
from uuid import uuid4

import aiohttp
from redis.asyncio import Redis
from redis.exceptions import WatchError
from aiohttp import web, ClientSession, ClientTimeout
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

MODEL_API_KEY = os.environ['MODEL_API_KEY']
MODEL_BASE_URL = os.environ['MODEL_BASE_URL']
MODEL_NAME = os.environ['MODEL_NAME']

EMBEDDING_BASE_URL = os.environ['EMBEDDING_BASE_URL']
EMBEDDING_API_KEY = os.environ['EMBEDDING_API_KEY']
EMBEDDING_NAME = os.environ['EMBEDDING_NAME']

REDIS_URL = os.environ['REDIS_URL']

BROADCAST_CHANNEL = "conn:events"
SUCCESS_COUNT_KEY = "llm:stats:success"
FAIL_COUNT_KEY = "llm:stats:fail"
TOTAL_TOKENS_KEY = "llm:stats:tokens"
STATS_MONTH_KEY = "llm:stats:month"
REALTIME_STATS_CHANNEL = "llm:stats:realtime"
PROXY_REQUEST_PATHS = {"/v1/chat/completions", "/v1/embeddings"}
STATIC_DIR = Path(__file__).resolve().parent / "static"


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


async def publish_proxy_error(
        redis,
        request,
        upstream_path: str,
        error_message: str,
        *,
        stage: str,
        status: int,
):
    await redis.publish(BROADCAST_CHANNEL, json.dumps({
        "type": "proxy_error",
        "conn_id": request.get("conn_id"),
        "remote": request.remote,
        "path": request.path,
        "upstream_path": upstream_path,
        "stage": stage,
        "status": status,
        "error": error_message,
        "ts": int(time.time() * 1000)
    }, ensure_ascii=False))


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
        if request.path == "/v1/chat/completions":
            if "deepseek" in proxy_req["model"]:
                proxy_req["model"] = MODEL_NAME
            if "qwen3" in proxy_req["model"]:
                proxy_req["model"] = MODEL_NAME
            elif "gpt-4o-mini" in proxy_req["model"]:
                proxy_req["model"] = MODEL_NAME
        elif request.path == "/v1/embeddings":
            proxy_req["model"] = EMBEDDING_NAME
        request["proxy_req"] = proxy_req

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
            "status": status,
            "cost_ms": int((time.time() - start_ts) * 1000),
            "ts": int(time.time() * 1000)
        }, ensure_ascii=False))
    return resp


async def proxy_json_request(request, upstream_path: str, base_url: str, api_key: str):
    proxy_req = request.get("proxy_req")
    if not proxy_req:
        return web.json_response({"error": "Invalid request"}, status=400)

    session = request.app["client_session"]
    try:
        async with session.post(
                f"{base_url}{upstream_path}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=proxy_req,
                timeout=ClientTimeout(total=300)
        ) as resp:
            if resp.status != 200:
                error_message = f"LLM API Error: {await resp.text()}"
                await publish_proxy_error(
                    request.app["redis"],
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

            if not proxy_req.get("stream"):
                try:
                    result = await resp.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    response_text = await resp.text()
                    error_message = f"Response parse error: {response_text or str(exc)}"
                    await publish_proxy_error(
                        request.app["redis"],
                        request,
                        upstream_path,
                        error_message,
                        stage="response_parse",
                        status=502,
                    )
                    await update_stats(request.app["redis"], success=False)
                    return web.json_response({"error": error_message}, status=502)
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
            async for chunk in resp.content:
                chunk_str = chunk.decode("utf-8", errors="ignore")
                if "data: " in chunk_str and "[DONE]" not in chunk_str:
                    try:
                        payload = json.loads(chunk_str.split("data: ")[-1].strip())
                        if "usage" in payload:
                            last_usage = payload["usage"]
                    except (json.JSONDecodeError, KeyError):
                        pass
                await response.write(chunk)

            await update_stats(
                request.app["redis"],
                success=True,
                tokens=last_usage.get("total_tokens", 0) if last_usage else 0
            )
            await response.write_eof()
            return response

    except Exception as exc:
        await publish_proxy_error(
            request.app["redis"],
            request,
            upstream_path,
            f"Internal error: {str(exc)}",
            stage="proxy_exception",
            status=500,
        )
        await update_stats(request.app["redis"], success=False)
        return web.json_response({"error": "Internal error"}, status=500)


async def chat(request):
    return await proxy_json_request(
        request,
        "/v1/chat/completions",
        MODEL_BASE_URL,
        MODEL_API_KEY,
    )


async def embeddings(request):
    return await proxy_json_request(
        request,
        "/v1/embeddings",
        EMBEDDING_BASE_URL,
        EMBEDDING_API_KEY,
    )


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
