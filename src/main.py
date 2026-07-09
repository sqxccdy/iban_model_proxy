import os
import time
import json
import logging
from redis.asyncio import Redis
from redis.exceptions import WatchError
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO)

API_KEY = os.environ['MODEL_API_KEY']
BASE_URL = os.environ['MODEL_BASE_URL']
REDIS_URL = os.environ['REDIS_URL']

BROADCAST_CHANNEL = "conn:events"
SUCCESS_COUNT_KEY = "llm:stats:success"
FAIL_COUNT_KEY = "llm:stats:fail"
TOTAL_TOKENS_KEY = "llm:stats:tokens"
STATS_MONTH_KEY = "llm:stats:month"
REALTIME_STATS_CHANNEL = "llm:stats:realtime"


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


@web.middleware
async def conn_broadcast_middleware(request, handler):
    start_ts = time.time()
    redis = request.app["redis"]
    req_info = None

    # 只处理chat接口的请求，精准提取你要的信息
    if request.path == "/v1/chat/completions" and request.method == "POST":
        try:
            # 直接读json，存到request上下文里，后面接口直接用，不重复读！
            chat_req = await request.json()
            request["chat_req"] = chat_req

            # 👇 你要的3样东西，简简单单
            req_info = {
                "model": chat_req.get("model"),
                # messages只取前2条，每条content截前50字，就是简略内容
                "messages_preview": [
                    {
                        "role": m.get("role"),
                        "content": (m.get("content") or "")[:50] + ("..." if len(m.get("content") or "") > 50 else "")
                    }
                    for m in chat_req.get("messages", [])[:2]
                ],
                # 除了model和messages的其他参数，就是你要的特殊参数
                "extra_params": {k: v for k, v in chat_req.items() if k not in ("model", "messages")}
            }
        except Exception:
            req_info = {"error": "invalid chat request"}

    # conn_open直接带上你要的信息！
    await redis.publish(BROADCAST_CHANNEL, json.dumps({
        "type": "conn_open",
        "remote": request.remote,
        "path": request.path,
        "request_info": req_info,  # 全是你想要的~
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
            "remote": request.remote,
            "path": request.path,
            "status": status,
            "cost_ms": int((time.time() - start_ts) * 1000),
            "ts": int(time.time() * 1000)
        }, ensure_ascii=False))
    return resp


async def chat(request):
    # 直接用中间件存的请求，不用再读json，超快！
    chat_req = request.get("chat_req")
    if not chat_req:
        return web.json_response({"error": "Invalid request"}, status=400)

    session = request.app["client_session"]
    try:
        async with session.post(
            f"{BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=chat_req,
            timeout=ClientTimeout(total=300)
        ) as resp:
            if resp.status != 200:
                await update_stats(request.app["redis"], success=False)
                return web.json_response(
                    {"error": f"LLM API Error: {await resp.text()}"},
                    status=resp.status
                )

            if not chat_req.get("stream"):
                result = await resp.json()
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

    except Exception:
        await update_stats(request.app["redis"], success=False)
        return web.json_response({"error": "Internal error"}, status=500)


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
                await resp.drain()
    finally:
        await pubsub.unsubscribe(BROADCAST_CHANNEL, REALTIME_STATS_CHANNEL)
        await pubsub.close()
    return resp


async def startup(app):
    app["redis"] = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    app["client_session"] = ClientSession(timeout=ClientTimeout(total=300))


async def cleanup(app):
    await app["redis"].close()
    await app["client_session"].close()


app = web.Application(middlewares=[conn_broadcast_middleware])
app.router.add_post("/v1/chat/completions", chat)
app.router.add_get("/events", events)

app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

if __name__ == "__main__":
    web.run_app(app, port=8080, access_log=logging.getLogger("aiohttp.access"))
