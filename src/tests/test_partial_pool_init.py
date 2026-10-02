"""MariaDBServer 가 일부 인스턴스의 풀 생성 실패를 견디는지 검증한다 — DB 없이 mock 으로 돈다.

실행: PYTHONPATH=src python -m unittest -v tests.test_partial_pool_init  (src 의 상위가 아니라 임의 cwd 에서)
"""

import unittest
from unittest import mock

import server
from config import InstanceConfig

PASSWORD = "pw-must-not-leak-7f3a"


def cfg(host, user="app", password=PASSWORD):
    return InstanceConfig(host=host, port=3306, user=user, password=password, db="appdb")


class FakePool:
    def close(self):
        pass

    async def wait_closed(self):
        pass


class PartialPoolInitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.srv = server.MariaDBServer()
        self.down = set()

        async def fake_create(**params):
            if params["host"] in self.down:
                raise OSError(f"Can't connect to MySQL server on '{params['host']}'")
            return FakePool()

        self.create = mock.AsyncMock(side_effect=fake_create)

    def use(self, default, **instances):
        data = {"default_instance": default, "instances": instances}
        return mock.patch.multiple(server, load_instances=mock.Mock(return_value=data), create_safe_pool=self.create)

    async def list_instances(self):
        self.srv.register_tools()
        tools = await self.srv.mcp.get_tools()
        return await tools["list_instances"].fn()

    async def test_all_instances_up(self):
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid")):
            await self.srv.initialize_pools()
        self.assertEqual(set(self.srv.pools), {"a", "b"})
        # 수정 전 코드에도 같은 결과를 기대하는 건강성 대조군이라 새 속성에 기대지 않는다.
        self.assertEqual(getattr(self.srv, "failed_instances", {}), {})

    async def test_one_instance_down_others_work(self):
        self.down = {"b.example.invalid"}
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid")):
            await self.srv.initialize_pools()
            self.assertIsInstance(await self.srv._get_pool("a"), FakePool)
            with self.assertRaisesRegex(ValueError, r"'b' is unavailable: .*b\.example\.invalid"):
                await self.srv._get_pool("b")
            listed = await self.list_instances()
        self.assertTrue(listed["a"]["available"])
        self.assertFalse(listed["b"]["available"])
        self.assertIn("b.example.invalid", listed["b"]["error"])

    async def test_all_instances_down_raises(self):
        self.down = {"a.example.invalid", "b.example.invalid"}
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid")):
            with self.assertRaises(ConnectionError):
                await self.srv.initialize_pools()

    async def test_default_instance_down_still_starts(self):
        self.down = {"a.example.invalid"}
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid")):
            await self.srv.initialize_pools()
            self.assertIsInstance(await self.srv._get_pool("b"), FakePool)
            with self.assertRaisesRegex(ValueError, "'a' is unavailable"):
                await self.srv._get_pool()

    async def test_failed_instance_is_retried_after_interval(self):
        self.down = {"b.example.invalid"}
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid")):
            await self.srv.initialize_pools()
            calls = self.create.await_count
            self.srv.POOL_RETRY_INTERVAL_SEC = 3600
            with self.assertRaises(ValueError):
                await self.srv._get_pool("b")
            self.assertEqual(self.create.await_count, calls, "간격 전에는 다시 만들지 않는다")

            self.down.clear()
            self.srv.POOL_RETRY_INTERVAL_SEC = 0
            self.assertIsInstance(await self.srv._get_pool("b"), FakePool)
            self.assertNotIn("b", self.srv.failed_instances)

    async def test_missing_password_is_permanent_and_never_leaks(self):
        self.down = {"c.example.invalid"}
        with self.use("a", a=cfg("a.example.invalid"), b=cfg("b.example.invalid", password=""),
                      c=cfg("c.example.invalid")):
            await self.srv.initialize_pools()
            self.srv.POOL_RETRY_INTERVAL_SEC = 0
            before = self.create.await_count
            with self.assertRaises(ValueError) as err_b:
                await self.srv._get_pool("b")
            self.assertEqual(self.create.await_count, before, "설정 오류는 재시도하지 않는다")
            with self.assertRaises(ValueError) as err_c:
                await self.srv._get_pool("c")
            listed = await self.list_instances()
        texts = [str(err_b.exception), str(err_c.exception), repr(self.srv.failed_instances), repr(listed)]
        for t in texts:
            self.assertNotIn(PASSWORD, t)


if __name__ == "__main__":
    unittest.main()
