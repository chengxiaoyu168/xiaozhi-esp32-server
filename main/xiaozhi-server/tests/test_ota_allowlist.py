"""OTA 允许列表与固件 2.5.2 的 token 握手。

不启动公网服务，也不读取任何真实密钥。token 用测试用 auth_key 在本地签。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

import websockets
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import config.settings as settings_mod
from core.api.ota_handler import OTAHandler
from core.auth import AuthManager
from core.utils.cache.config import CacheType
from core.utils.cache.manager import cache_manager
from core.websocket_server import WebSocketServer


def _prime_runtime_config(config: dict) -> None:
    """连接建立时会无参调用 setup_logging()。生产进程里启动阶段已经写入缓存。"""
    settings_mod.config_file_valid = True
    cache_manager.set(CacheType.CONFIG, "main_config", config)


def _server_config(port: int, *, enabled: bool, allowed, expire_seconds=3600):
    return {
        "server": {
            "ip": "127.0.0.1",
            "port": port,
            "http_port": 8003,
            "auth_key": "unit-test-auth-key",
            "auth": {
                "enabled": enabled,
                "allowed_devices": list(allowed),
                "expire_seconds": expire_seconds,
            },
            "websocket": f"ws://127.0.0.1:{port}/xiaozhi/v1/",
            "vision_explain": "http://127.0.0.1:8003/mcp/vision/explain",
            "timezone_offset": 8,
        },
        "log": {
            "log_level": "ERROR",
            "log_dir": "tmp",
            "log_file": "pytest-ota.log",
            "data_dir": "data",
            "log_format": "{message}",
            "log_format_file": "{message}",
        },
        "selected_module": {
            "VAD": "SileroVAD",
            "ASR": "Stub",
            "LLM": "Mock",
            "TTS": "Stub",
            "Memory": "nomem",
            "Intent": "nointent",
            "VLLM": "noop",
        },
        "exit_commands": ["退出"],
        "wakeup_words": [],
        "delete_audio": True,
        "xiaozhi": {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        },
        "Memory": {"nomem": {"type": "nomem"}},
        "Intent": {"nointent": {"type": "nointent"}},
        "prompt": "你是测试机器人。",
        "read_config_from_api": False,
    }


def _arm_bootstrap(config, directory) -> str:
    secret = secrets.token_urlsafe(32)
    path = directory / ".ota-bootstrap"
    path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    config["server"]["auth"]["ota_bootstrap_file"] = str(path)
    return secret


async def _ota(config, device_id, client_id="client-1", bootstrap=None):
    handler = OTAHandler(config)
    app = web.Application()
    app.router.add_post("/xiaozhi/ota/", handler.handle_post)
    app.router.add_post(
        r"/xiaozhi/ota/{bootstrap:[A-Za-z0-9_-]{32,128}}/",
        handler.handle_post,
    )
    target = "/xiaozhi/ota/" if not bootstrap else f"/xiaozhi/ota/{bootstrap}/"
    async with TestClient(TestServer(app, access_log=None)) as client:
        response = await client.post(
            target,
            headers={"Device-Id": device_id, "Client-Id": client_id},
            json={
                "application": {"version": "2.5.2"},
                "board": {"type": "esp32-c3"},
            },
        )
        text = await response.text()
        body = json.loads(text) if text else {}
        return response.status, body


def test_ota_signs_only_allowlisted_devices_and_token_roundtrips(tmp_path):
    config = _server_config(0, enabled=True, allowed=["AA:BB:CC:DD:EE:10"])
    secret = _arm_bootstrap(config, tmp_path)
    _prime_runtime_config(config)

    async def _run():
        status, body = await _ota(config, "aa:bb:cc:dd:ee:10", "client-a", secret)
        assert status == 200, body
        token = body["websocket"]["token"]
        assert token
        assert "Bearer" not in token
        assert secret not in json.dumps(body)
        assert body["websocket"]["url"].endswith("/xiaozhi/v1/")

        denied, denied_body = await _ota(
            config, "aa:bb:cc:dd:ee:99", "client-x", secret
        )
        assert denied == 403
        assert denied_body.get("message") == "device not allowlisted"
        assert "websocket" not in denied_body
        assert "token" not in json.dumps(denied_body)

        auth = AuthManager(config["server"]["auth_key"], expire_seconds=3600)
        assert auth.verify_token(token, client_id="client-a", username="aa:bb:cc:dd:ee:10")
        # 固件把 Client-Id / Device-Id 原样送回，大小写必须和签发时一致。
        assert not auth.verify_token(
            token, client_id="client-a", username="AA:BB:CC:DD:EE:10"
        )

    asyncio.run(_run())


def test_auth_disabled_keeps_open_ota_without_a_token():
    config = _server_config(0, enabled=False, allowed=[])
    _prime_runtime_config(config)

    async def _run():
        status, body = await _ota(config, "aa:bb:cc:dd:ee:10")
        assert status == 200
        assert body["websocket"]["token"] == ""

    asyncio.run(_run())


def test_empty_allowlist_fails_closed(tmp_path):
    config = _server_config(0, enabled=True, allowed=[])
    secret = _arm_bootstrap(config, tmp_path)
    _prime_runtime_config(config)

    async def _run():
        status, body = await _ota(config, "aa:bb:cc:dd:ee:10", bootstrap=secret)
        assert status == 403
        assert "token" not in json.dumps(body)
        assert secret not in json.dumps(body)

    asyncio.run(_run())


def test_token_expiry_and_renewal_and_stable_auth_key():
    short = AuthManager("unit-test-auth-key", expire_seconds=1)
    token = short.generate_token("client-a", "aa:bb:cc:dd:ee:10")
    assert short.verify_token(token, "client-a", "aa:bb:cc:dd:ee:10")
    # 过期判断是整数秒的严格大于，1 秒有效期要跨过两个整数秒边界。
    time.sleep(2.2)
    assert not short.verify_token(token, "client-a", "aa:bb:cc:dd:ee:10")
    renewed = short.generate_token("client-a", "aa:bb:cc:dd:ee:10")
    assert short.verify_token(renewed, "client-a", "aa:bb:cc:dd:ee:10")

    same = AuthManager("unit-test-auth-key", expire_seconds=3600)
    other = AuthManager("different-local-key", expire_seconds=3600)
    stable = same.generate_token("client-a", "aa:bb:cc:dd:ee:10")
    assert same.verify_token(stable, "client-a", "aa:bb:cc:dd:ee:10")
    assert not other.verify_token(stable, "client-a", "aa:bb:cc:dd:ee:10")


def _install_yielding_sleep():
    """conftest 把 asyncio.sleep 换成立即返回，连接上的超时循环会占满事件循环。"""
    previous = asyncio.sleep

    async def _yielding_sleep(seconds=0, result=None):
        if not seconds or float(seconds) <= 0:
            return result
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def _finish():
            if not future.done():
                future.set_result(result)

        loop.call_later(float(seconds), _finish)
        return await future

    asyncio.sleep = _yielding_sleep
    return previous


def test_websocket_requires_ota_token_even_for_allowlisted_device(monkeypatch):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = _server_config(port, enabled=True, allowed=["aa:bb:cc:dd:ee:10"])
    _prime_runtime_config(config)

    import core.websocket_server as ws_mod

    monkeypatch.setattr(
        ws_mod,
        "initialize_modules",
        lambda *args, **kwargs: {
            "llm": None,
            "vad": None,
            "asr": None,
            "intent": None,
            "memory": None,
        },
    )

    async def _hello(headers, *, speak: bool = True):
        uri = f"ws://127.0.0.1:{port}/xiaozhi/v1/"
        try:
            async with websockets.connect(
                uri,
                additional_headers=headers,
                ping_interval=None,
                open_timeout=5,
            ) as ws:
                # 认证失败在 hello 之前就下发并关闭。先读，避免把关闭当成丢包。
                if not speak:
                    message = await asyncio.wait_for(ws.recv(), timeout=5)
                    return "open", message
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "version": 1,
                            "transport": "websocket",
                            "audio_params": {
                                "format": "opus",
                                "sample_rate": 16000,
                                "channels": 1,
                                "frame_duration": 60,
                            },
                        }
                    )
                )
                message = await asyncio.wait_for(ws.recv(), timeout=5)
                return "open", message
        except Exception as exc:
            return "closed", str(exc)

    async def _run():
        server = WebSocketServer(config)
        task = asyncio.create_task(server.start())
        try:
            def _port_open() -> bool:
                probe = socket.socket()
                probe.settimeout(0.2)
                try:
                    probe.connect(("127.0.0.1", port))
                    return True
                except OSError:
                    return False
                finally:
                    probe.close()

            for _ in range(50):
                if await asyncio.to_thread(_port_open):
                    break
                await asyncio.to_thread(time.sleep, 0.05)
            else:
                raise RuntimeError("ws did not start")

            auth = AuthManager("unit-test-auth-key", expire_seconds=3600)
            token = auth.generate_token("client-a", "aa:bb:cc:dd:ee:10")
            ok_state, ok_body = await _hello(
                {
                    "Device-Id": "aa:bb:cc:dd:ee:10",
                    "Client-Id": "client-a",
                    "Authorization": f"Bearer {token}",
                }
            )
            assert ok_state == "open", ok_body
            assert "session_id" in ok_body

            missing_state, missing_body = await _hello(
                {"Device-Id": "aa:bb:cc:dd:ee:10", "Client-Id": "client-a"},
                speak=False,
            )
            assert missing_body == "认证失败"
            assert missing_state == "open"

            bad_state, bad_body = await _hello(
                {
                    "Device-Id": "aa:bb:cc:dd:ee:10",
                    "Client-Id": "client-a",
                    "Authorization": "Bearer not-a-valid-token",
                },
                speak=False,
            )
            assert bad_body == "认证失败"
            assert bad_state == "open"

            stranger = auth.generate_token("client-a", "aa:bb:cc:dd:ee:99")
            stranger_state, stranger_body = await _hello(
                {
                    "Device-Id": "aa:bb:cc:dd:ee:99",
                    "Client-Id": "client-a",
                    "Authorization": f"Bearer {stranger}",
                },
                speak=False,
            )
            assert stranger_body == "认证失败"
            assert stranger_state == "open"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    previous_sleep = _install_yielding_sleep()
    try:
        asyncio.run(_run())
    finally:
        asyncio.sleep = previous_sleep
