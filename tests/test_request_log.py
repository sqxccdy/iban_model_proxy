import asyncio
import json
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from src.main import request_log_middleware


class FakeRedis:
    def __init__(self):
        self.messages = []

    async def publish(self, channel, message):
        self.messages.append((channel, json.loads(message)))


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_405_is_published_as_request_log(self):
        redis = FakeRedis()
        app = web.Application()
        app["redis"] = redis
        app["broadcast_tasks"] = set()
        transport = mock.Mock()
        transport.get_extra_info.return_value = ("127.0.0.1", 51968)
        request = make_mocked_request("POST", "/health/", app=app, transport=transport)

        async def method_not_allowed(_request):
            raise web.HTTPMethodNotAllowed("POST", ["GET"])

        with self.assertRaises(web.HTTPMethodNotAllowed):
            await request_log_middleware(request, method_not_allowed)
        if app["broadcast_tasks"]:
            await asyncio.gather(*app["broadcast_tasks"])

        self.assertEqual(len(redis.messages), 1)
        payload = redis.messages[0][1]
        self.assertEqual(payload["type"], "request_log")
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["path"], "/health/")
        self.assertEqual(payload["status"], 405)
        self.assertEqual(payload["reason"], "Method Not Allowed")
        self.assertEqual(payload["remote"], "127.0.0.1:51968")
        self.assertRegex(payload["http_version"], r"^HTTP/\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
