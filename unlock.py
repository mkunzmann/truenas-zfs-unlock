#!/usr/bin/env python3
"""
TrueNAS ZFS Unlock Script
Unlocks encrypted ZFS datasets via the TrueNAS JSON-RPC 2.0 over WebSocket API.
"""

import asyncio
import json
import os
import ssl
import sys

try:
    import websockets
except ImportError:
    websockets = None


def load_secrets():
    """Load all docker secrets (env vars starting with FILE__) into environment."""
    for key, val in list(os.environ.items()):
        if key.startswith("FILE__"):
            target_var = key[len("FILE__"):]
            if not os.path.isfile(val):
                print(f"Secret file does not exist: {val}", file=sys.stderr)
                sys.exit(1)
            try:
                with open(val, "r", encoding="utf-8") as f:
                    secret = f.read().strip()
                os.environ[target_var] = secret
            except Exception as e:
                print(f"Error reading secret file {val}: {e}", file=sys.stderr)
                sys.exit(1)


def get_config():
    """Load configuration from environment variables and dataset file."""
    load_secrets()

    truenas_host = os.environ.get("TRUENAS_HOST")
    if not truenas_host:
        print("Error: Missing 'TRUENAS_HOST' environment variable", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("TRUENAS_API_KEY")
    if not api_key:
        print("Error: 'TRUENAS_API_KEY' has not been set as env var or docker secret", file=sys.stderr)
        sys.exit(1)

    skip_cert_verify = os.environ.get("SKIP_CERT_VERIFY", "").lower() == "true"

    datasets_file = os.environ.get("DATASETS_FILE")
    datasets = []

    if datasets_file:
        if not os.path.isfile(datasets_file):
            print(f"Error: Datasets mapping file '{datasets_file}' does not exist", file=sys.stderr)
            sys.exit(1)
        with open(datasets_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().replace("\r", "")
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                dataset_path, passphrase = line.split("=", 1)
                dataset_path = dataset_path.strip().strip("/")
                passphrase = passphrase.strip()
                if not passphrase:
                    print(f"Warning: No passphrase found for dataset '{dataset_path}'. Skipping...")
                    continue
                datasets.append((dataset_path, passphrase))
    else:
        # Fallback to ZFS__<pool>__<dataset> env vars for backward compatibility
        for key, val in os.environ.items():
            if key.startswith("ZFS__"):
                suffix = key[len("ZFS__"):]
                parts = suffix.split("__")
                dataset_path = "/".join(parts)
                passphrase = val.strip()
                if passphrase:
                    datasets.append((dataset_path, passphrase))

        if not datasets:
            print("Error: Missing 'DATASETS_FILE' environment variable", file=sys.stderr)
            sys.exit(1)

    return {
        "host": truenas_host,
        "api_key": api_key,
        "skip_cert_verify": skip_cert_verify,
        "datasets": datasets,
    }


def build_ws_urls(host: str) -> list:
    """
    Construct WebSocket URLs to try in order.
    Supports host with or without scheme, port, and custom path.
    """
    host = host.strip()
    scheme = "wss"

    if host.startswith("https://"):
        scheme = "wss"
        host = host[len("https://"):]
    elif host.startswith("http://"):
        scheme = "ws"
        host = host[len("http://"):]
    elif host.startswith("wss://"):
        scheme = "wss"
        host = host[len("wss://"):]
    elif host.startswith("ws://"):
        scheme = "ws"
        host = host[len("ws://"):]

    host = host.rstrip("/")

    # If the user specified an explicit path (e.g. /api/current or /websocket)
    if "/" in host:
        host_part, custom_path = host.split("/", 1)
        return [f"{scheme}://{host_part}/{custom_path}"]

    # Default endpoints: prefer /api/current (JSON-RPC 2.0), fallback to /websocket
    return [
        f"{scheme}://{host}/api/current",
        f"{scheme}://{host}/websocket",
    ]


class TrueNASWSClient:
    """TrueNAS JSON-RPC 2.0 over WebSocket Client."""

    def __init__(self, host: str, api_key: str, skip_cert_verify: bool = False, timeout: float = 30.0):
        self.host = host
        self.api_key = api_key
        self.skip_cert_verify = skip_cert_verify
        self.timeout = timeout
        self.ws = None
        self.req_id = 0

    async def connect(self):
        """Connect to TrueNAS WebSocket endpoint."""
        if websockets is None:
            raise RuntimeError(
                "The 'websockets' Python library is required. "
                "Please ensure 'py3-websockets' or 'websockets' is installed."
            )
        urls = build_ws_urls(self.host)
        last_err = None

        for url in urls:
            ssl_ctx = None
            if url.startswith("wss://"):
                ssl_ctx = ssl.create_default_context()
                if self.skip_cert_verify:
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE

            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(url, ssl=ssl_ctx, max_size=10 * 1024 * 1024),
                    timeout=10.0,
                )
                return
            except Exception as e:
                last_err = e
                continue

        raise ConnectionError(f"Failed to connect to TrueNAS WebSocket at {urls}: {last_err}")

    async def close(self):
        """Close WebSocket connection."""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def call(self, method: str, params=None) -> dict:
        """Call a TrueNAS JSON-RPC method and wait for response matching call id."""
        if params is None:
            params = []
        self.req_id += 1
        current_id = self.req_id
        payload = {
            "jsonrpc": "2.0",
            "id": current_id,
            "method": method,
            "params": params,
        }

        await asyncio.wait_for(self.ws.send(json.dumps(payload)), timeout=self.timeout)

        # Loop until receiving response matching current_id
        while True:
            raw_msg = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            # Check if this message is the response to our call
            if msg.get("id") == current_id:
                if "error" in msg:
                    error_data = msg["error"]
                    err_msg = error_data.get("message", "Unknown error")
                    err_code = error_data.get("code")
                    raise RuntimeError(f"TrueNAS API error ({err_code}): {err_msg} (details: {error_data})")
                return msg

            # Other messages are server notifications (e.g., collection_update); keep waiting

    async def authenticate(self):
        """Authenticate session using TrueNAS API key."""
        try:
            resp = await self.call("auth.login_with_api_key", [self.api_key])
            if resp.get("result") is True:
                return
        except RuntimeError as e:
            # Fallback for future versions where auth.login_with_api_key might be removed
            username = os.environ.get("TRUENAS_USERNAME")
            if username:
                resp = await self.call(
                    "auth.login_ex",
                    [
                        {
                            "mechanism": "API_KEY_PLAIN",
                            "username": username,
                            "api_key": self.api_key,
                        }
                    ],
                )
                if resp.get("result", {}).get("response_type") == "SUCCESS":
                    return
            raise e

        raise RuntimeError("Authentication failed: invalid API key")

    async def is_locked(self, dataset_path: str) -> bool:
        """Check if dataset is encrypted and locked."""
        try:
            resp = await self.call("pool.dataset.query", [[["id", "=", dataset_path]]])
            results = resp.get("result", [])
            if not results:
                print(f"Error: dataset '{dataset_path}' not found", file=sys.stderr)
                return False

            dataset_info = results[0]
            if not dataset_info.get("encrypted", False):
                print(f"{dataset_path} is not encrypted")
                return False

            locked = dataset_info.get("locked")
            if locked is True:
                return True
            elif locked is False:
                print(f"{dataset_path} is already unlocked")
                return False
            else:
                print(f"Error: got an unexpected value '{locked}' for {dataset_path} 'locked' status", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Error checking status for {dataset_path}: {e}", file=sys.stderr)
            return False

    async def unlock(self, dataset_path: str, passphrase: str) -> bool:
        """Unlock dataset using passphrase."""
        options = {
            "key_file": False,
            "recursive": False,
            "force": True,
            "toggle_attachments": True,
            "datasets": [
                {
                    "name": dataset_path,
                    "passphrase": passphrase,
                }
            ],
        }

        try:
            resp = await self.call("pool.dataset.unlock", [dataset_path, options])
            result = resp.get("result", {})
            unlocked = result.get("unlocked", [])
            failed = result.get("failed", {})

            if dataset_path in unlocked:
                print(f"Successfully unlocked {dataset_path}")
                return True
            elif failed and dataset_path in failed:
                print(f"Failed to unlock {dataset_path}: {failed[dataset_path]}")
                return False
            else:
                print(f"Unlock operation returned: {result}")
                return bool(unlocked)
        except Exception as e:
            print(f"Error unlocking {dataset_path}: {e}", file=sys.stderr)
            return False


async def main_async():
    print("Starting unlock script - ws version")
    config = get_config()

    client = TrueNASWSClient(
        host=config["host"],
        api_key=config["api_key"],
        skip_cert_verify=config["skip_cert_verify"],
    )

    try:
        await client.connect()
        await client.authenticate()
    except Exception as e:
        print(f"Error connecting/authenticating to TrueNAS: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        for dataset_path, passphrase in config["datasets"]:
            print(f"\nDiscovered {dataset_path}, attempting to unlock now")
            if await client.is_locked(dataset_path):
                print(f"{dataset_path} is locked, attempting to unlock...")
                await client.unlock(dataset_path, passphrase)
    finally:
        await client.close()

    print("\nFinished unlock script, exiting...")


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
