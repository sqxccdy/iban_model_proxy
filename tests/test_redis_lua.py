"""Optional real-Redis check for the atomic daily ledger; needs redis-server/redis-cli."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from src.key_registry import REGISTRY_SCRIPT
from src.stats import STATS_SCRIPT


@unittest.skipUnless(os.environ.get("RUN_REDIS_LUA") == "1", "set RUN_REDIS_LUA=1 to run a temporary Redis")
class RedisLuaTests(unittest.TestCase):
    def test_daily_environment_project_month_and_retry(self):
        if not shutil.which("redis-server") or not shutil.which("redis-cli"):
            self.skipTest("redis-server and redis-cli are required")
        with tempfile.TemporaryDirectory(prefix="model-proxy-lua-", dir="/private/tmp") as directory:
            socket = str(Path(directory) / "redis.sock")
            stats_script = Path(directory) / "stats.lua"
            registry_script = Path(directory) / "registry.lua"
            stats_script.write_text(STATS_SCRIPT)
            registry_script.write_text(REGISTRY_SCRIPT)
            server = subprocess.Popen([
                "redis-server", "--port", "0", "--unixsocket", socket,
                "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                "--dir", directory,
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(100):
                    if Path(socket).exists():
                        break
                    if server.poll() is not None:
                        raise RuntimeError(f"temporary Redis failed to start: {server.stdout.read()}")
                    time.sleep(0.02)
                else:
                    raise RuntimeError("temporary Redis socket was not created")

                def cli(*args):
                    run = subprocess.run(["redis-cli", "-s", socket, "--raw", *map(str, args)],
                                         capture_output=True, text=True, check=True)
                    if run.stdout.startswith("ERR "):
                        raise AssertionError(run.stdout)
                    return run.stdout.strip()

                def registry(action, key, value="", parent=""):
                    return cli("--eval", registry_script, "monitor:authorization:mappings",
                               "monitor:project:mappings", "monitor:project:owners", ",",
                               action, key, value, parent)

                self.assertEqual(registry("save_environment", "env", "生产"), "ok")
                project = json.dumps({"name": "订单项目", "environment_authorization": "env"})
                self.assertEqual(registry("save_project", "project", project, "env"), "ok")
                self.assertEqual(registry("save_environment", "project", "冲突"), "project_key")
                self.assertEqual(registry("delete_environment", "env"), "has_projects")
                self.assertEqual(registry("save_project", "project", project, "other"), "missing_environment")
                self.assertEqual(registry("save_environment", "other", "测试"), "ok")
                self.assertEqual(registry("save_project", "project", project, "other"), "different_environment")

                keys = ["llm:stats:month", "llm:stats:success", "llm:stats:fail", "llm:stats:tokens",
                        "llm:stats:dimension:keys", "llm:stats:batch_ids", "llm:stats:realtime"]
                batch = [
                    {"day": "2026-09-29", "dimensions": {"authorization": "env-id"},
                     "success": 1, "fail": 0, "tokens": 12, "input_tokens": 8, "output_tokens": 4, "duration_ms": 1000},
                    {"day": "2026-09-29", "dimensions": {"authorization": "env-id", "project": "project-id"},
                     "success": 1, "fail": 1, "tokens": 6, "input_tokens": 3, "output_tokens": 3, "duration_ms": 5000},
                ]

                def stats(month, items, batch_id, ledger_month=None):
                    return cli("--eval", stats_script, *keys, ",", month, json.dumps(items),
                               1000, batch_id, ledger_month or month)

                self.assertEqual(stats("2026-09", batch, "batch-a"), "1")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:tokens", "global"), "18")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:tokens", "authorization:env-id"), "18")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:tokens", "project:project-id"), "6")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:input_tokens", "global"), "11")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:output_tokens", "global"), "7")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:duration_ms", "global"), "6000")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:success", "global"), "2")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:fail", "global"), "1")
                self.assertEqual(stats("2026-09", batch, "batch-a"), "0")
                self.assertEqual(cli("GET", "llm:stats:tokens"), "18")

                october = [{"day": "2026-10-01", "dimensions": {"authorization": "env-id"},
                            "success": 1, "fail": 0, "tokens": 4, "input_tokens": 3, "output_tokens": 1, "duration_ms": 100}]
                self.assertEqual(stats("2026-10", october, "batch-b"), "1")
                self.assertEqual(cli("GET", "llm:stats:tokens"), "4")
                self.assertEqual(stats("2026-10", batch, "batch-a", "2026-09"), "0")
                self.assertEqual(cli("HGET", "llm:stats:daily:2026-09-29:tokens", "global"), "18")
                self.assertEqual(cli("GET", "llm:stats:month"), "2026-10")
            finally:
                server.terminate()
                server.wait(timeout=5)
                server.stdout.close()


if __name__ == "__main__":
    unittest.main()
