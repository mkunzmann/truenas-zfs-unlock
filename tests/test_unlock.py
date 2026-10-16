#!/usr/bin/env python3
"""Unit tests for TrueNAS ZFS Unlock Script."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import unlock


class TestSecretsAndConfig(unittest.TestCase):
    """Test secrets loading and configuration parsing."""

    def setUp(self):
        self.orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_load_secrets_success(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("my-secret-key-123\n")
            f_path = f.name

        try:
            os.environ["FILE__TRUENAS_API_KEY"] = f_path
            unlock.load_secrets()
            self.assertEqual(os.environ.get("TRUENAS_API_KEY"), "my-secret-key-123")
        finally:
            os.remove(f_path)

    def test_load_secrets_missing_file(self):
        os.environ["FILE__TRUENAS_API_KEY"] = "/nonexistent/path/to/secret"
        with self.assertRaises(SystemExit) as cm:
            unlock.load_secrets()
        self.assertEqual(cm.exception.code, 1)

    def test_get_config_missing_host(self):
        os.environ.clear()
        with self.assertRaises(SystemExit):
            unlock.get_config()

    def test_get_config_missing_api_key(self):
        os.environ["TRUENAS_HOST"] = "192.168.1.100"
        if "TRUENAS_API_KEY" in os.environ:
            del os.environ["TRUENAS_API_KEY"]
        with self.assertRaises(SystemExit):
            unlock.get_config()

    def test_get_config_datasets_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# This is a comment\n")
            f.write("\n")
            f.write("tank/photos=photos_secret_pass\r\n")
            f.write("  tank/documents  =  docs_pass  \n")
            f.write("tank/empty_pass=\n")
            f_path = f.name

        try:
            os.environ["TRUENAS_HOST"] = "192.168.1.100"
            os.environ["TRUENAS_API_KEY"] = "test-key"
            os.environ["SKIP_CERT_VERIFY"] = "true"
            os.environ["DATASETS_FILE"] = f_path

            config = unlock.get_config()
            self.assertEqual(config["host"], "192.168.1.100")
            self.assertEqual(config["api_key"], "test-key")
            self.assertTrue(config["skip_cert_verify"])
            self.assertEqual(
                config["datasets"],
                [
                    ("tank/photos", "photos_secret_pass"),
                    ("tank/documents", "docs_pass"),
                ],
            )
        finally:
            os.remove(f_path)

    def test_get_config_fallback_zfs_env_vars(self):
        os.environ["TRUENAS_HOST"] = "192.168.1.100"
        os.environ["TRUENAS_API_KEY"] = "test-key"
        if "DATASETS_FILE" in os.environ:
            del os.environ["DATASETS_FILE"]
        os.environ["ZFS__tank__backup"] = "backup_pass"

        config = unlock.get_config()
        self.assertIn(("tank/backup", "backup_pass"), config["datasets"])


class TestUrlConstruction(unittest.TestCase):
    """Test WebSocket URL construction."""

    def test_build_ws_urls_simple_ip(self):
        urls = unlock.build_ws_urls("192.168.1.1")
        self.assertEqual(
            urls,
            [
                "wss://192.168.1.1/api/current",
                "wss://192.168.1.1/websocket",
            ],
        )

    def test_build_ws_urls_with_port(self):
        urls = unlock.build_ws_urls("nas.local:8443")
        self.assertEqual(
            urls,
            [
                "wss://nas.local:8443/api/current",
                "wss://nas.local:8443/websocket",
            ],
        )

    def test_build_ws_urls_with_https_scheme(self):
        urls = unlock.build_ws_urls("https://nas.local:443/")
        self.assertEqual(
            urls,
            [
                "wss://nas.local:443/api/current",
                "wss://nas.local:443/websocket",
            ],
        )

    def test_build_ws_urls_with_http_scheme(self):
        urls = unlock.build_ws_urls("http://nas.local:80")
        self.assertEqual(
            urls,
            [
                "ws://nas.local:80/api/current",
                "ws://nas.local:80/websocket",
            ],
        )

    def test_build_ws_urls_with_custom_path(self):
        urls = unlock.build_ws_urls("wss://nas.local/api/v25.10")
        self.assertEqual(urls, ["wss://nas.local/api/v25.10"])


class TestTrueNASWSClient(unittest.IsolatedAsyncioTestCase):
    """Test TrueNASWSClient methods with mocked WebSocket connection."""

    async def test_call_success(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        mock_ws = AsyncMock()
        client.ws = mock_ws

        # Server returns response matching req_id
        mock_ws.recv.return_value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": True})

        resp = await client.call("auth.login_with_api_key", ["test-key"])
        self.assertEqual(resp, {"jsonrpc": "2.0", "id": 1, "result": True})
        mock_ws.send.assert_awaited_once_with(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "auth.login_with_api_key", "params": ["test-key"]})
        )

    async def test_call_ignores_notifications(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        mock_ws = AsyncMock()
        client.ws = mock_ws

        # First returns a notification (no matching id), then returns the actual response
        notification = json.dumps({"jsonrpc": "2.0", "method": "collection_update", "params": {"msg": "added"}})
        response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"unlocked": ["tank/data"]}})
        mock_ws.recv.side_effect = [notification, response]

        resp = await client.call("pool.dataset.unlock", ["tank/data", {}])
        self.assertEqual(resp.get("result"), {"unlocked": ["tank/data"]})
        self.assertEqual(mock_ws.recv.await_count, 2)

    async def test_call_raises_on_error(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        mock_ws = AsyncMock()
        client.ws = mock_ws

        mock_ws.recv.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Method call error"}}
        )

        with self.assertRaises(RuntimeError) as cm:
            await client.call("some.method")
        self.assertIn("Method call error", str(cm.exception))

    async def test_authenticate_success(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": True})

        await client.authenticate()
        client.call.assert_awaited_once_with("auth.login_with_api_key", ["test-key"])

    async def test_authenticate_failure(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": False})

        with self.assertRaises(RuntimeError) as cm:
            await client.authenticate()
        self.assertIn("Authentication failed", str(cm.exception))

    async def test_is_locked_when_locked(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": [{"id": "tank/photos", "encrypted": True, "locked": True}],
            }
        )

        locked = await client.is_locked("tank/photos")
        self.assertTrue(locked)
        client.call.assert_awaited_once_with("pool.dataset.query", [[["id", "=", "tank/photos"]]])

    async def test_is_locked_when_unlocked(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": [{"id": "tank/photos", "encrypted": True, "locked": False}],
            }
        )

        locked = await client.is_locked("tank/photos")
        self.assertFalse(locked)

    async def test_is_locked_not_encrypted(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": [{"id": "tank/unencrypted", "encrypted": False, "locked": False}],
            }
        )

        locked = await client.is_locked("tank/unencrypted")
        self.assertFalse(locked)

    async def test_unlock_success(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"unlocked": ["tank/photos"], "failed": {}},
            }
        )

        success = await client.unlock("tank/photos", "secret")
        self.assertTrue(success)
        client.call.assert_awaited_once()
        args, kwargs = client.call.call_args
        self.assertEqual(args[0], "pool.dataset.unlock")
        self.assertEqual(args[1][0], "tank/photos")
        self.assertEqual(args[1][1]["datasets"][0]["passphrase"], "secret")

    async def test_unlock_failure(self):
        client = unlock.TrueNASWSClient(host="nas.local", api_key="test-key")
        client.call = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"unlocked": [], "failed": {"tank/photos": "Incorrect passphrase"}},
            }
        )

        success = await client.unlock("tank/photos", "wrong_secret")
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
