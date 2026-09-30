"""Asynchronous, extensible request counters backed by Redis."""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo


STAT_TIMEZONE = ZoneInfo("Asia/Shanghai")
METRICS = ("success", "fail", "tokens", "input_tokens", "output_tokens", "duration_ms")


def current_stats_day():
    return datetime.now(STAT_TIMEZONE).strftime("%Y-%m-%d")


def extract_usage(payload):
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return response["usage"]
    return None


def usage_counts(payload):
    usage = extract_usage(payload)
    if usage is None and isinstance(payload, dict):
        usage = payload
    usage = usage or {}
    def count(*names):
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0
    input_tokens = count("input_tokens", "prompt_tokens")
    output_tokens = count("output_tokens", "completion_tokens")
    total_tokens = count("total_tokens") or input_tokens + output_tokens
    return total_tokens, input_tokens, output_tokens


STATS_SCRIPT = """
local month = ARGV[1]
local stored_month = redis.call('GET', KEYS[1])
if not stored_month or stored_month < month then
    local old_keys = redis.call('SMEMBERS', KEYS[5])
    for _, key in ipairs(old_keys) do redis.call('DEL', key) end
    redis.call('DEL', KEYS[5])
    redis.call('SET', KEYS[1], month)
    redis.call('SET', KEYS[2], 0)
    redis.call('SET', KEYS[3], 0)
    redis.call('SET', KEYS[4], 0)
    redis.call('DEL', KEYS[6])
elseif stored_month > month then
    month = stored_month
end

local ledger = KEYS[6] .. ':' .. ARGV[5]
if redis.call('SADD', ledger, ARGV[4]) == 0 then return 0 end
redis.call('EXPIRE', ledger, 7776000)
local batch = cjson.decode(ARGV[2])
local success = 0
local fail = 0
local tokens = 0
for _, item in ipairs(batch) do
    local in_month = string.sub(item.day, 1, 7) == month
    if in_month then
        success = success + item.success
        fail = fail + item.fail
        tokens = tokens + item.tokens
    end
    local metrics = {success=item.success, fail=item.fail, tokens=item.tokens,
        input_tokens=item.input_tokens, output_tokens=item.output_tokens,
        duration_ms=item.duration_ms}
    for metric, count in pairs(metrics) do
        if count > 0 then
            local daily_key = 'llm:stats:daily:' .. item.day .. ':' .. metric
            redis.call('HINCRBY', daily_key, 'global', count)
            for dimension, value in pairs(item.dimensions) do
                redis.call('HINCRBY', daily_key, dimension .. ':' .. value, count)
            end
            if item.dimensions.authorization and item.dimensions.model then
                redis.call('HINCRBY', daily_key,
                    'environment_model:' .. item.dimensions.authorization .. ':' .. item.dimensions.model, count)
            end
            if item.dimensions.project and item.dimensions.model then
                redis.call('HINCRBY', daily_key,
                    'project_model:' .. item.dimensions.project .. ':' .. item.dimensions.model, count)
            end
        end
    end
    for dimension, value in pairs(item.dimensions) do
        for metric, count in pairs(metrics) do
            if in_month and count > 0 then
                local key = 'llm:stats:dimension:' .. dimension .. ':' .. metric
                redis.call('SADD', KEYS[5], key)
                redis.call('HINCRBY', key, value, count)
            end
        end
    end
    if in_month and item.dimensions.authorization and item.dimensions.model then
        for metric, count in pairs(metrics) do
            if count > 0 then
                local key = 'llm:stats:dimension:environment_model:' .. metric
                redis.call('SADD', KEYS[5], key)
                redis.call('HINCRBY', key,
                    item.dimensions.authorization .. ':' .. item.dimensions.model, count)
            end
        end
    end
    if in_month and item.dimensions.project and item.dimensions.model then
        for metric, count in pairs(metrics) do
            if count > 0 then
                local key = 'llm:stats:dimension:project_model:' .. metric
                redis.call('SADD', KEYS[5], key)
                redis.call('HINCRBY', key,
                    item.dimensions.project .. ':' .. item.dimensions.model, count)
            end
        end
    end
end
local totals = {
    success_count = redis.call('INCRBY', KEYS[2], success),
    fail_count = redis.call('INCRBY', KEYS[3], fail),
    total_tokens = redis.call('INCRBY', KEYS[4], tokens),
}
local by_authorization = {}
for _, metric in ipairs({'success', 'fail', 'tokens'}) do
    local rows = redis.call('HGETALL', 'llm:stats:dimension:authorization:' .. metric)
    for i = 1, #rows, 2 do
        local authorization = rows[i]
        if not by_authorization[authorization] then by_authorization[authorization] = {success=0, fail=0, tokens=0} end
        by_authorization[authorization][metric] = tonumber(rows[i + 1])
    end
end
local by_model = {}
for _, metric in ipairs({'success', 'fail', 'tokens'}) do
    local rows = redis.call('HGETALL', 'llm:stats:dimension:model:' .. metric)
    for i = 1, #rows, 2 do
        local model = rows[i]
        if not by_model[model] then by_model[model] = {success=0, fail=0, tokens=0} end
        by_model[model][metric] = tonumber(rows[i + 1])
    end
end
local by_project_model = {}
for _, metric in ipairs({'success', 'fail', 'tokens', 'input_tokens', 'output_tokens', 'duration_ms'}) do
    local rows = redis.call('HGETALL', 'llm:stats:dimension:project_model:' .. metric)
    for i = 1, #rows, 2 do
        local project, model = string.match(rows[i], '^([^:]+):(.+)$')
        if project and model then
            if not by_project_model[project] then by_project_model[project] = {} end
            if not by_project_model[project][model] then
                by_project_model[project][model] = {success=0, fail=0, tokens=0,
                    input_tokens=0, output_tokens=0, duration_ms=0}
            end
            by_project_model[project][model][metric] = tonumber(rows[i + 1])
        end
    end
end
local by_environment_model = {}
for _, metric in ipairs({'success', 'fail', 'tokens', 'input_tokens', 'output_tokens', 'duration_ms'}) do
    local rows = redis.call('HGETALL', 'llm:stats:dimension:environment_model:' .. metric)
    for i = 1, #rows, 2 do
        local environment, model = string.match(rows[i], '^([^:]+):(.+)$')
        if environment and model then
            if not by_environment_model[environment] then by_environment_model[environment] = {} end
            if not by_environment_model[environment][model] then
                by_environment_model[environment][model] = {success=0, fail=0, tokens=0,
                    input_tokens=0, output_tokens=0, duration_ms=0}
            end
            by_environment_model[environment][model][metric] = tonumber(rows[i + 1])
        end
    end
end
totals.type = 'stats_update'
totals.by_authorization = by_authorization
totals.by_model = by_model
totals.by_project_model = by_project_model
totals.by_environment_model = by_environment_model
totals.month = month
totals.ts = tonumber(ARGV[3])
redis.call('PUBLISH', KEYS[7], cjson.encode(totals))
return 1
"""


async def get_stats_snapshot(redis, keys, authorization_labels=None, project_labels=None, day=None, model_names=None):
    """Read a consistent monthly snapshot for a newly opened monitor page."""
    day = day or current_stats_day()
    month = current_stats_day()[:7]
    async with redis.pipeline() as pipe:
        pipe.get(keys[0])
        pipe.mget(*keys[1:4])
        for metric in ("success", "fail", "tokens"):
            pipe.hgetall(f"llm:stats:dimension:authorization:{metric}")
        for metric in ("success", "fail", "tokens"):
            pipe.hgetall(f"llm:stats:dimension:model:{metric}")
        for metric in METRICS:
            pipe.hgetall(f"llm:stats:dimension:project_model:{metric}")
        for metric in METRICS:
            pipe.hgetall(f"llm:stats:dimension:environment_model:{metric}")
        for metric in METRICS:
            pipe.hgetall(f"llm:stats:daily:{day}:{metric}")
        results = await pipe.execute()
        stored_month, totals, successes, failures, tokens, model_successes, model_failures, model_tokens = results[:8]
        project_model_rows = results[8:8 + len(METRICS)]
        environment_model_rows = results[8 + len(METRICS):8 + 2 * len(METRICS)]
        daily_rows = results[8 + 2 * len(METRICS):]

    authorization_labels = authorization_labels or {}
    project_labels = project_labels or {}
    model_names = set(model_names or ())
    for rows in daily_rows:
        model_names.update(key[len("model:"):] for key in rows if key.startswith("model:"))
        for key in rows:
            for dimension in ("project_model", "environment_model"):
                if key.startswith(dimension + ":"):
                    _, _, model = key[len(dimension) + 1:].partition(":")
                    if model:
                        model_names.add(model)
    if stored_month == month:
        model_names.update(model_successes)
        model_names.update(model_failures)
        model_names.update(model_tokens)
        for rows in [*project_model_rows, *environment_model_rows]:
            for key in rows:
                _, _, model = key.partition(":")
                if model:
                    model_names.add(model)
    by_authorization = {key: {"success": 0, "fail": 0, "tokens": 0} for key in authorization_labels}
    by_model = {name: {"success": 0, "fail": 0, "tokens": 0} for name in sorted(model_names)}
    by_project_model = {project: {name: {metric: 0 for metric in METRICS} for name in sorted(model_names)}
                        for project in project_labels}
    by_environment_model = {environment: {name: {metric: 0 for metric in METRICS} for name in sorted(model_names)}
                            for environment in authorization_labels}
    if stored_month != month:
        totals = (0, 0, 0)
    else:
        for metric, rows in (("success", successes), ("fail", failures), ("tokens", tokens)):
            for key, count in rows.items():
                if key in by_authorization:
                    by_authorization[key][metric] = int(count)
        for metric, rows in (("success", model_successes), ("fail", model_failures), ("tokens", model_tokens)):
            for name, count in rows.items():
                by_model[name][metric] = int(count)
        for metric, rows in zip(METRICS, project_model_rows):
            for key, count in rows.items():
                project, _, model = key.partition(":")
                if project in by_project_model and model in by_project_model[project]:
                    by_project_model[project][model][metric] = int(count)
        for metric, rows in zip(METRICS, environment_model_rows):
            for key, count in rows.items():
                environment, _, model = key.partition(":")
                if environment in by_environment_model and model in by_environment_model[environment]:
                    by_environment_model[environment][model][metric] = int(count)

    daily = {
        "date": day,
        "global": {metric: 0 for metric in METRICS},
        "environments": {key: {metric: 0 for metric in METRICS} for key in authorization_labels},
        "projects": {key: {metric: 0 for metric in METRICS} for key in project_labels},
        "models": {name: {metric: 0 for metric in METRICS} for name in sorted(model_names)},
        "project_models": {project: {name: {metric: 0 for metric in METRICS} for name in sorted(model_names)}
                           for project in project_labels},
        "environment_models": {environment: {name: {metric: 0 for metric in METRICS} for name in sorted(model_names)}
                               for environment in authorization_labels},
    }
    for metric, rows in zip(METRICS, daily_rows):
        daily["global"][metric] = int(rows.get("global", 0))
        for key in authorization_labels:
            daily["environments"][key][metric] = int(rows.get(f"authorization:{key}", 0))
        for key in project_labels:
            daily["projects"][key][metric] = int(rows.get(f"project:{key}", 0))
        for name in model_names:
            daily["models"][name][metric] = int(rows.get(f"model:{name}", 0))
        for project in project_labels:
            for name in model_names:
                daily["project_models"][project][name][metric] = int(rows.get(f"project_model:{project}:{name}", 0))
        for environment in authorization_labels:
            for name in model_names:
                daily["environment_models"][environment][name][metric] = int(rows.get(f"environment_model:{environment}:{name}", 0))
    metric_groups = [daily["global"], *daily["environments"].values(),
                     *daily["projects"].values(), *daily["models"].values()]
    metric_groups.extend(counts for models in daily["project_models"].values() for counts in models.values())
    metric_groups.extend(counts for models in daily["environment_models"].values() for counts in models.values())
    metric_groups.extend(counts for models in by_project_model.values() for counts in models.values())
    metric_groups.extend(counts for models in by_environment_model.values() for counts in models.values())
    for counts in metric_groups:
        calls = counts["success"] + counts["fail"]
        counts["calls"] = calls
        counts["success_rate"] = round(100 * counts["success"] / calls, 2) if calls else None
        counts["avg_duration_ms"] = round(counts["duration_ms"] / calls, 2) if calls else None
    daily["timezone"] = "Asia/Shanghai"

    return {
        "type": "stats_update",
        "month": month,
        "success_count": int(totals[0] or 0),
        "fail_count": int(totals[1] or 0),
        "total_tokens": int(totals[2] or 0),
        "by_authorization": by_authorization,
        "by_model": by_model,
        "by_project_model": by_project_model,
        "by_environment_model": by_environment_model,
        "authorizations": authorization_labels,
        "projects": project_labels,
        "daily": daily,
        "ts": int(time.time() * 1000),
    }


class StatsRecorder:
    """Coalesce updates in memory; Redis I/O only runs in the background worker."""

    def __init__(self, redis, keys, channel):
        self.redis = redis
        self.keys = keys
        self.channel = channel
        self.pending = defaultdict(lambda: [0] * len(METRICS))
        self.wakeup = asyncio.Event()
        self.worker = None
        self.stopping = False

    def start(self):
        self.worker = asyncio.create_task(self._run())

    def record(self, success: bool, tokens=0, dimensions=None, *, input_tokens=0,
               output_tokens=0, duration_ms=0, day=None):
        dimensions = dimensions or {}
        # Dimension names form Redis keys; values remain hash fields.
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in dimensions):
            raise ValueError("Invalid statistics dimension name")
        if any(not isinstance(value, str) for value in dimensions.values()):
            raise ValueError("Invalid statistics dimension value")
        day = day or current_stats_day()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Invalid statistics day")
        key = (day, tuple(sorted(dimensions.items())))
        counts = self.pending[key]
        counts[0 if success else 1] += 1
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            counts[2] += tokens
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens > 0:
            counts[3] += input_tokens
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens > 0:
            counts[4] += output_tokens
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms > 0:
            counts[5] += duration_ms
        self.wakeup.set()

    async def _run(self):
        while True:
            await self.wakeup.wait()
            self.wakeup.clear()
            batch = self.pending
            self.pending = defaultdict(lambda: [0] * len(METRICS))
            if not batch and self.stopping:
                return
            # A short delay folds concurrent requests into one Redis transaction.
            await asyncio.sleep(0.02)
            if self.pending:
                for dimensions, counts in self.pending.items():
                    merged = batch[dimensions]
                    for i in range(len(METRICS)):
                        merged[i] += counts[i]
                self.pending = defaultdict(lambda: [0] * len(METRICS))
                self.wakeup.clear()
            batch_id = uuid4().hex
            ledger_month = current_stats_day()[:7]
            payload = [
                {"day": day, "dimensions": dict(dimensions), "success": counts[0],
                 "fail": counts[1], "tokens": counts[2], "input_tokens": counts[3],
                 "output_tokens": counts[4], "duration_ms": counts[5]}
                for (day, dimensions), counts in batch.items()
            ]
            while True:
                try:
                    await self.redis.eval(
                        STATS_SCRIPT, len(self.keys) + 1, *self.keys, self.channel,
                        current_stats_day()[:7],
                        json.dumps(payload, ensure_ascii=False), int(time.time() * 1000), batch_id, ledger_month,
                    )
                    break
                except Exception:
                    logging.exception("Statistics flush failed; retrying")
                    await asyncio.sleep(1)
            if self.stopping and not self.pending:
                return

    async def close(self):
        self.stopping = True
        self.wakeup.set()
        try:
            await asyncio.wait_for(self.worker, timeout=5)
        except asyncio.TimeoutError:
            logging.error("Statistics could not be flushed before shutdown")
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
