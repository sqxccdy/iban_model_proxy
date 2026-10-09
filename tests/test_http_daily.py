"""Optional end-to-end check using a temporary Redis and a local fake upstream."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("RUN_HTTP_DAILY") == "1", "set RUN_HTTP_DAILY=1 for local HTTP integration")
class DailyHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_daily_metrics_from_environment_and_project_keys(self):
        if not shutil.which("redis-server"):
            self.skipTest("redis-server is required")
        from aiohttp import ClientSession, web
        from aiohttp.test_utils import TestClient, TestServer
        from redis.asyncio import Redis
        from src import main
        from src.stats import StatsRecorder, current_stats_day

        with tempfile.TemporaryDirectory(prefix="model-proxy-http-", dir="/private/tmp") as directory:
            socket = str(Path(directory) / "redis.sock")
            server = subprocess.Popen([
                "redis-server", "--port", "0", "--unixsocket", socket,
                "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                "--dir", directory,
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            upstream = client = proxy = session = redis = None
            old_routes = main.MODEL_ROUTES
            try:
                for _ in range(100):
                    if Path(socket).exists():
                        break
                    if server.poll() is not None:
                        raise RuntimeError(f"temporary Redis failed: {server.stdout.read()}")
                    await asyncio.sleep(0.02)
                else:
                    raise RuntimeError("temporary Redis did not start")

                async def upstream_handler(request):
                    body = await request.json()
                    if body.get("fail"):
                        return web.json_response({"error": "simulated upstream failure"}, status=503)
                    if request.path == "/v1/responses":
                        return web.json_response({"response": {"usage": {
                            "input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}, "output": []})
                    if body.get("stream"):
                        self.assertTrue(body.get("stream_options", {}).get("include_usage"))
                        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                        await response.prepare(request)
                        await response.write(b'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}\n\n')
                        await response.write(b'data: [DONE]\n\n')
                        await response.write_eof()
                        return response
                    return web.json_response({"choices": [{"message": {"content": "ok"}}], "usage": {
                        "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}})

                upstream_app = web.Application()
                upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
                upstream_app.router.add_post("/v1/responses", upstream_handler)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                main.MODEL_ROUTES = {"test-model": {
                    "style": "chat", "base_url": str(upstream.make_url("/")).rstrip("/"),
                    "api_key": "unused", "upstream_model": "test-model",
                    "chat_template_kwargs": {}, "stream_include_usage": True,
                }}
                main.MODEL_ROUTES["other-model"] = dict(main.MODEL_ROUTES["test-model"], upstream_model="other-model")

                redis = Redis(unix_socket_path=socket, encoding="utf-8", decode_responses=True)
                session = ClientSession()
                proxy_app = web.Application(middlewares=[main.conn_broadcast_middleware])
                proxy_app["redis"] = redis
                proxy_app["client_session"] = session
                proxy_app["broadcast_tasks"] = set()
                recorder = StatsRecorder(redis, [main.STATS_MONTH_KEY, main.SUCCESS_COUNT_KEY,
                    main.FAIL_COUNT_KEY, main.TOTAL_TOKENS_KEY, main.STATS_DIMENSION_KEYS,
                    main.STATS_BATCH_IDS], main.REALTIME_STATS_CHANNEL)
                proxy_app["stats"] = recorder
                recorder.start()
                proxy_app.router.add_post("/v1/chat/completions", main.chat)
                proxy_app.router.add_post("/v1/responses", main.responses)
                proxy_app.router.add_get("/stats", main.stats)
                proxy_app.router.add_post("/authorization-mappings", main.authorization_mappings)
                proxy_app.router.add_post("/project-mappings", main.project_mappings)
                proxy = TestServer(proxy_app)
                client = TestClient(proxy)
                await client.start_server()

                response = await client.post("/authorization-mappings", json={
                    "authorization": "env-key", "description": "生产环境"})
                self.assertEqual(response.status, 200)
                response = await client.post("/project-mappings", json={
                    "authorization": "project-key", "name": "订单项目",
                    "environment_authorization": "env-key"})
                self.assertEqual(response.status, 200)

                response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer project-key"},
                    json={"model": "test-model", "messages": []})
                self.assertEqual(response.status, 200)
                await response.read()
                response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer project-key"},
                    json={"model": "test-model", "messages": [], "stream": True})
                self.assertEqual(response.status, 200)
                self.assertIn("[DONE]", await response.text())
                response = await client.post("/v1/responses", headers={"Authorization": "env-key"},
                    json={"model": "test-model", "input": "hello"})
                self.assertEqual(response.status, 200)
                await response.read()
                response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer unknown"},
                    json={"model": "test-model", "messages": [], "fail": True})
                self.assertEqual(response.status, 503)
                await response.read()
                response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer project-key"},
                    json={"model": "other-model", "messages": []})
                self.assertEqual(response.status, 200)
                await response.read()

                for _ in range(100):
                    response = await client.get(f"/stats?date={current_stats_day()}")
                    snapshot = await response.json()
                    if snapshot["daily"]["global"]["calls"] == 5:
                        break
                    await asyncio.sleep(0.03)
                else:
                    self.fail("daily counters did not flush")
                daily = snapshot["daily"]
                self.assertEqual(daily["global"]["tokens"], 36)
                self.assertEqual(daily["global"]["input_tokens"], 24)
                self.assertEqual(daily["global"]["output_tokens"], 12)
                self.assertEqual(daily["global"]["calls"], 5)
                self.assertEqual(daily["global"]["success_rate"], 80)
                self.assertIsInstance(daily["global"]["avg_duration_ms"], (int, float))
                self.assertGreaterEqual(daily["global"]["avg_duration_ms"], 0)
                self.assertEqual(len(daily["environments"]), 1)
                self.assertEqual(len(daily["projects"]), 1)
                self.assertEqual(snapshot["by_model"]["test-model"], {"success": 3, "fail": 1, "tokens": 24})
                self.assertEqual(snapshot["by_model"]["other-model"], {"success": 1, "fail": 0, "tokens": 12})
                self.assertEqual(daily["models"]["test-model"]["input_tokens"], 16)
                self.assertEqual(daily["models"]["test-model"]["calls"], 4)
                self.assertEqual(daily["models"]["other-model"]["tokens"], 12)
                project_id = next(iter(daily["projects"]))
                self.assertEqual(snapshot["by_project_model"][project_id]["test-model"]["tokens"], 17)
                self.assertEqual(snapshot["by_project_model"][project_id]["test-model"]["input_tokens"], 11)
                self.assertEqual(daily["project_models"][project_id]["test-model"]["calls"], 2)
                self.assertEqual(snapshot["by_project_model"][project_id]["other-model"]["tokens"], 12)
                self.assertEqual(daily["project_models"][project_id]["other-model"]["calls"], 1)
                environment_id = next(iter(daily["environments"]))
                self.assertEqual(snapshot["by_environment_model"][environment_id]["test-model"]["tokens"], 24)
                self.assertEqual(snapshot["by_environment_model"][environment_id]["other-model"]["tokens"], 12)
                self.assertEqual(daily["environment_models"][environment_id]["test-model"]["calls"], 3)
                environment = next(iter(daily["environments"].values()))
                project = next(iter(daily["projects"].values()))
                self.assertEqual((environment["tokens"], environment["calls"]), (36, 4))
                self.assertEqual((project["tokens"], project["calls"]), (29, 3))
                self.assertEqual(project["input_tokens"], 19)
                self.assertEqual(project["output_tokens"], 10)
                self.assertNotIn("project-key", json.dumps(snapshot))
            finally:
                main.MODEL_ROUTES = old_routes
                if client is not None:
                    await client.close()
                if proxy is not None:
                    await proxy.close()
                if upstream is not None:
                    await upstream.close()
                if session is not None:
                    await session.close()
                if redis is not None:
                    if proxy is not None and "stats" in proxy.app:
                        await proxy.app["stats"].close()
                    await redis.aclose()
                server.terminate()
                server.wait(timeout=5)
                server.stdout.close()


if __name__ == "__main__":
    unittest.main()
