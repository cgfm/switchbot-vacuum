"""K10+ regional hosts.

No account, network or robot is used.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.switchbot_vacuum import k10_api as api

US = "https://wonderlabs.us.api.switchbot.net"


class RegionTests(unittest.TestCase):
    def test_known_regions(self):
        self.assertEqual(api.botslab_region("https://wonderlabs.eu.api.switchbot.net"), "eu1")
        self.assertEqual(api.botslab_region(US + "/"), "na1")
        self.assertEqual(api.botslab_region("https://wonderlabs.ap.api.switchbot.net"), "ap1")

    def test_unknown_or_foreign_hosts(self):
        for host in (None, "", "https://attacker.example", "http://wonderlabs.us.api.switchbot.net",
                     "https://wonderlabs.us.api.switchbot.net.attacker.example",
                     "https://wonderlabs.zz.api.switchbot.net"):
            self.assertIsNone(api.botslab_region(host), host)

    def test_storage_url_must_match_region(self):
        self.assertTrue(api.trusted_storage_url(
            "https://na1-ali-1days.oss-us-east-1.aliyuncs.com/x?sig=1", "na1"))
        self.assertTrue(api.trusted_storage_url(
            "https://eu1-ali-180days.oss-eu-central-1.aliyuncs.com/x", "eu1"))
        for url in (
            "https://eu1-ali-1days.oss-eu-central-1.aliyuncs.com/x",
            "https://na1-x.attacker.example/x",
            "https://na1-x.aliyuncs.com/x",
            "https://na1-ali-1days.attacker.aliyuncs.com/x",
            "https://na1-ali-1days.oss-us-east-1.aliyuncs.com.attacker.example/x",
            "http://na1-ali-1days.oss-us-east-1.aliyuncs.com/x",
        ):
            self.assertFalse(api.trusted_storage_url(url, "na1"), url)

    def test_upload_accepts_regional_bucket(self):
        info = {
            "provider": "aliyun", "bucket": "na1-ali-1days",
            "endpoint": "oss-us-east-1.aliyuncs.com", "path": "a/b.txt",
            "sts": {"accesskey_id": "k", "accesskey_secret": "s", "security_token": "t"},
        }
        url, _, robot_url = api.oss_request(info, b"{}", region="na1")
        self.assertTrue(url.startswith("https://na1-ali-1days.oss-us-east-1.aliyuncs.com/"))
        self.assertEqual(robot_url, "aliyun://na1-ali-1days.oss-us-east-1.aliyuncs.com/a/b.txt")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info, b"{}", region="eu1")
        with self.assertRaisesRegex(api.K10ProtocolError, "'aws'"):
            api.oss_request(info | {"provider": "aws"}, b"{}", region="na1")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info | {"endpoint": "attacker.example"}, b"{}", region="na1")


class Response:
    def __init__(self, *, data=None, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.data

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(data=next(self.replies))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(data=next(self.replies))


class RegionalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_us_account_uses_us_hosts(self):
        client = api.K10Api("example-device", "example-product", "installation")
        session = Session([
            {"statusCode": 100, "body": {"token": "delegated", "valid_time": 9999999999}},
            {"code": 0},
        ])
        await client.authorize(session, US, "account-token", "family")
        await client.request(session, "/v1/iot/device/get_property")
        self.assertEqual(session.calls[0][1], US + "/wonder/sweeper360/v1/auth/applyToken")
        self.assertEqual(session.calls[1][1],
                         "https://na1-sapp-api.botslab.com/v1/iot/device/get_property")

    async def test_region_change_discards_token(self):
        client = api.K10Api("example-device", "example-product", "installation")
        client.token, client.token_expiry = "eu-token", 9999999999
        session = Session([{"statusCode": 100, "body": {"token": "us", "valid_time": 9999999999}}])
        await client.authorize(session, US, "account-token", "family")
        self.assertEqual((client.region, client.token), ("na1", "us"))

    async def test_unknown_region_is_rejected_before_any_request(self):
        client = api.K10Api("example-device", "example-product", "installation")
        session = Session()
        with self.assertRaises(api.K10ProtocolError):
            await client.authorize(session, "https://wonderlabs.zz.api.switchbot.net",
                                   "account-token", "family")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
