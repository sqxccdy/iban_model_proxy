import asyncio
import json
import unittest

from src.stats import StatsRecorder, get_stats_snapshot, current_stats_day, usage_counts, METRICS


class WaitingRedis:
    def __init__(self):
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def eval(self, *args):
        self.calls.append(args)
        self.called.set()
        await self.release.wait()
        return 1


class SnapshotPipeline:
    def __init__(self, data):
        self.data = data
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, key):
        self.commands.append(("get", key))
        return self

    def mget(self, *keys):
        self.commands.append(("mget", keys))
        return self

    def hgetall(self, key):
        self.commands.append(("hash", key))
        return self

    async def execute(self):
        return [([self.data.get(key) for key in value] if method == "mget"
                 else self.data.get(value, {} if method == "hash" else None))
                for method, value in self.commands]


class SnapshotRedis:
    def __init__(self, data):
        self.data = data

    def pipeline(self):
        return SnapshotPipeline(self.data)


class StatsRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_does_not_wait_for_redis_and_coalesces_all_metrics(self):
        redis = WaitingRedis()
        recorder = StatsRecorder(redis, ["month", "success", "fail", "tokens", "keys", "batches"], "events")
        recorder.start()
        dims = {"authorization": "environment-a", "project": "project-a"}
        recorder.record(True, 12, dims, input_tokens=8, output_tokens=4, duration_ms=100)
        recorder.record(False, 0, dims, duration_ms=300)
        await asyncio.wait_for(redis.called.wait(), 1)
        # The flush is still waiting for Redis; the request-side record remains immediate.
        recorder.record(True, 5, dims, input_tokens=3, output_tokens=2, duration_ms=200)
        self.assertEqual(sum(counts[0] for counts in recorder.pending.values()), 1)
        batch = json.loads(redis.calls[0][-4])
        self.assertEqual(batch, [{"day": current_stats_day(), "dimensions": dims,
            "success": 1, "fail": 1, "tokens": 12, "input_tokens": 8,
            "output_tokens": 4, "duration_ms": 400}])
        redis.release.set()
        await recorder.close()
        second = json.loads(redis.calls[1][-4])[0]
        self.assertEqual(second["tokens"], 5)
        self.assertEqual(second["duration_ms"], 200)

    async def test_days_do_not_merge_and_unknown_keys_have_only_global_dimension(self):
        recorder = StatsRecorder(None, [], "events")
        recorder.record(True, 7, {}, day="2026-09-30")
        recorder.record(True, 9, {}, day="2026-10-01")
        self.assertEqual(len(recorder.pending), 2)
        self.assertEqual(sum(value[2] for value in recorder.pending.values()), 16)

    async def test_snapshot_daily_hierarchy_derived_metrics_and_monthly_compatibility(self):
        day = "2026-09-12"
        data = {"month": current_stats_day()[:7], "success": "4", "fail": "1", "tokens": "31",
                "llm:stats:dimension:authorization:tokens": {"env-a": "31"}}
        for metric, value in zip(METRICS, (3, 1, 31, 20, 11, 1000)):
            data[f"llm:stats:daily:{day}:{metric}"] = {
                "global": str(value), "authorization:env-a": str(value), "project:project-a": str(value)}
        snapshot = await get_stats_snapshot(SnapshotRedis(data), ["month", "success", "fail", "tokens"],
                    {"env-a": "生产", "unused": "测试"}, {"project-a": {"name": "项目 A", "environment_id": "env-a"}}, day)
        daily = snapshot["daily"]
        self.assertEqual(snapshot["total_tokens"], 31)
        self.assertEqual(daily["date"], day)
        self.assertEqual(daily["environments"]["env-a"]["calls"], 4)
        self.assertEqual(daily["projects"]["project-a"]["success_rate"], 75)
        self.assertEqual(daily["projects"]["project-a"]["avg_duration_ms"], 250)
        self.assertEqual(daily["global"]["tokens"], 31)
        self.assertIsNone(daily["environments"]["unused"]["success_rate"])
        self.assertIsNone(daily["environments"]["unused"]["avg_duration_ms"])

    async def test_previous_month_totals_hidden_but_historical_daily_values_remain(self):
        data = {"month": "2000-01", "tokens": "99",
                "llm:stats:daily:2000-01-01:tokens": {"global": "99"}}
        snapshot = await get_stats_snapshot(SnapshotRedis(data), ["month", "success", "fail", "tokens"], day="2000-01-01")
        self.assertEqual(snapshot["total_tokens"], 0)
        self.assertEqual(snapshot["daily"]["global"]["tokens"], 99)


class UsageTests(unittest.TestCase):
    def test_chat_responses_embeddings_and_missing_usage(self):
        self.assertEqual(usage_counts({"usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}}), (12, 8, 4))
        self.assertEqual(usage_counts({"response": {"usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}}}), (9, 7, 2))
        self.assertEqual(usage_counts({"usage": {"prompt_tokens": 8, "total_tokens": 8}}), (8, 8, 0))
        self.assertEqual(usage_counts({"input_tokens": 7, "output_tokens": 2}), (9, 7, 2))
        self.assertEqual(usage_counts({"usage": None}), (0, 0, 0))
        self.assertEqual(usage_counts(None), (0, 0, 0))
        self.assertEqual(usage_counts({"usage": {"prompt_tokens": -1, "completion_tokens": True}}), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
