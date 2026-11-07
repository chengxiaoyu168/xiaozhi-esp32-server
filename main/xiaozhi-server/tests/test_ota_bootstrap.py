"""OTA 路径凭证。凭证在测试里临时生成，不写入仓库，也不出现在响应里。"""
from __future__ import annotations

import json
import os
import secrets

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.auth import AuthManager
from tests.test_ota_allowlist import _arm_bootstrap, _prime_runtime_config, _server_config


def _app(config):
    handler = OTAHandler(config)
    app = web.Application()
    app.router.add_post("/xiaozhi/ota/", handler.handle_post)
    app.router.add_post(
        r"/xiaozhi/ota/{bootstrap:[A-Za-z0-9_-]{32,128}}/",
        handler.handle_post,
    )
    return app


async def _post(client, path, device_id):
    response = await client.post(
        path,
        headers={"Device-Id": device_id, "Client-Id": "client-a"},
        json={"application": {"version": "2.5.2"}, "board": {"type": "esp32-c3"}},
    )
    text = await response.text()
    body = json.loads(text) if text else {}
    return response.status, body, text


def test_standard_path_and_wrong_secret_are_denied_valid_path_returns_wss(tmp_path):
    config = _server_config(0, enabled=True, allowed=["aa:bb:cc:dd:ee:10"])
    secret = _arm_bootstrap(config, tmp_path)
    public = tmp_path / ".public-wss"
    public.write_text("wss://tunnel.example/xiaozhi/v1/\n", encoding="utf-8")
    config["server"]["public_wss_file"] = str(public)
    _prime_runtime_config(config)
    wrong = secrets.token_urlsafe(32)
    assert wrong != secret
    captured = []
    logger = setup_logging()
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")

    async def _run():
        async with TestClient(TestServer(_app(config), access_log=None)) as client:
            plain_status, plain_body, plain_text = await _post(
                client, "/xiaozhi/ota/", "aa:bb:cc:dd:ee:10"
            )
            assert plain_status == 404
            assert "websocket" not in plain_body
            assert secret not in plain_text

            bad_status, bad_body, bad_text = await _post(
                client, f"/xiaozhi/ota/{wrong}/", "aa:bb:cc:dd:ee:10"
            )
            assert bad_status == 404
            assert "token" not in json.dumps(bad_body)
            assert secret not in bad_text

            ok_status, ok_body, ok_text = await _post(
                client, f"/xiaozhi/ota/{secret}/", "aa:bb:cc:dd:ee:10"
            )
            assert ok_status == 200, ok_body
            assert ok_body["websocket"]["url"] == "wss://tunnel.example/xiaozhi/v1/"
            assert ok_body["websocket"]["token"]
            assert secret not in ok_text
            auth = AuthManager(config["server"]["auth_key"], expire_seconds=3600)
            assert auth.verify_token(
                ok_body["websocket"]["token"], "client-a", "aa:bb:cc:dd:ee:10"
            )
            assert not auth.verify_token("not-a-token", "client-a", "aa:bb:cc:dd:ee:10")

            other_status, other_body, _other_text = await _post(
                client, f"/xiaozhi/ota/{secret}/", "aa:bb:cc:dd:ee:99"
            )
            assert other_status == 403
            assert "token" not in json.dumps(other_body)

            rotated = secrets.token_urlsafe(32)
            path = tmp_path / ".ota-bootstrap"
            path.write_text(rotated + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            revoked, _, _ = await _post(
                client, f"/xiaozhi/ota/{secret}/", "aa:bb:cc:dd:ee:10"
            )
            assert revoked == 404
            renewed, renewed_body, renewed_text = await _post(
                client, f"/xiaozhi/ota/{rotated}/", "aa:bb:cc:dd:ee:10"
            )
            assert renewed == 200
            assert renewed_body["websocket"]["token"]
            assert secret not in renewed_text
            assert rotated not in renewed_text

    try:
        import asyncio

        asyncio.run(_run())
    finally:
        logger.remove(sink)

    leaked = "".join(captured)
    assert secret not in leaked
    assert wrong not in leaked


def test_token_epoch_revokes_existing_websocket_tokens():
    issued = AuthManager("unit-test-auth-key", expire_seconds=3600, token_epoch=1)
    token = issued.generate_token("client-a", "aa:bb:cc:dd:ee:10")
    assert issued.verify_token(token, "client-a", "aa:bb:cc:dd:ee:10")
    rotated = AuthManager("unit-test-auth-key", expire_seconds=3600, token_epoch=2)
    assert not rotated.verify_token(token, "client-a", "aa:bb:cc:dd:ee:10")
    assert not AuthManager("unit-test-auth-key", expire_seconds=3600).verify_token(
        "bogus", "client-a", "aa:bb:cc:dd:ee:10"
    )
