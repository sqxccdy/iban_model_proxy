import json
import unittest

from src.key_registry import (
    AUTHORIZATION_MAPPING_KEY, PROJECT_MAPPING_KEY, PROJECT_OWNER_KEY,
    authorization_stats_id, get_authorization_context, get_authorization_description,
    get_authorization_mappings, get_project_mappings, mutate_registry,
    normalize_authorization,
)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def hmget(self, key, *fields):
        self.operations.append(("hmget", key, fields))
        return self

    def hget(self, key, field):
        self.operations.append(("hget", key, field))
        return self

    async def execute(self):
        result = []
        for method, key, fields in self.operations:
            mapping = self.redis.hashes.get(key, {})
            result.append([mapping.get(field) for field in fields] if method == "hmget" else mapping.get(fields))
        return result


class FakeRedis:
    def __init__(self, environments=None, projects=None):
        self.hashes = {
            AUTHORIZATION_MAPPING_KEY: environments or {},
            PROJECT_MAPPING_KEY: projects or {},
        }
        self.eval_calls = []

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def hmget(self, key, *fields):
        return [self.hashes.get(key, {}).get(field) for field in fields]

    def pipeline(self):
        return FakePipeline(self)

    async def eval(self, *args):
        self.eval_calls.append(args)
        return "ok"


class AuthorizationStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_and_raw_header_match_environment(self):
        redis = FakeRedis({"abc": "生产", "Bearer old": "旧配置"})
        self.assertEqual(await get_authorization_description(redis, "Bearer abc"), "生产")
        self.assertEqual(await get_authorization_description(redis, "bearer old"), "旧配置")
        self.assertEqual(await get_authorization_context(redis, "abc"),
                         ("生产", {"authorization": "abc", "environment_authorization": "abc"}))
        self.assertIsNone((await get_authorization_context(redis, "Bearer missing"))[0])
        self.assertEqual(await get_authorization_mappings(redis), [
            {"authorization": "abc", "description": "生产"},
            {"authorization": "old", "description": "旧配置"},
        ])
        self.assertEqual(normalize_authorization("bearer\tabc"), "abc")
        self.assertEqual(normalize_authorization("Token abc"), "Token abc")
        self.assertIsNone(normalize_authorization("Bearer   "))

    async def test_project_key_resolves_to_itself_and_parent_environment(self):
        project = json.dumps({"name": "项目 A", "environment_authorization": "abc"})
        redis = FakeRedis({"abc": "生产"}, {"project-key": project})
        name, context = await get_authorization_context(redis, "Bearer project-key")
        self.assertEqual(name, "项目 A")
        self.assertEqual(context["environment_authorization"], "abc")
        self.assertEqual(context["authorization"], "project-key")
        self.assertTrue(context["project"])
        self.assertNotEqual(authorization_stats_id("abc"), authorization_stats_id("project-key"))
        self.assertEqual((await get_project_mappings(redis, {"abc"}))[0]["name"], "项目 A")
        self.assertEqual(await get_project_mappings(redis, {"other"}), [])

    async def test_registry_uses_atomic_lua_for_relationship_changes(self):
        redis = FakeRedis()
        await mutate_registry(redis, "save_project", "project-key", '{"name":"项目 A"}', "abc")
        args = redis.eval_calls[0]
        self.assertEqual(args[1:5], (3, AUTHORIZATION_MAPPING_KEY, PROJECT_MAPPING_KEY, PROJECT_OWNER_KEY))
        self.assertEqual(args[-4:], ("save_project", "project-key", '{"name":"项目 A"}', "abc"))


if __name__ == "__main__":
    unittest.main()
