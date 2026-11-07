"""真实 WebSocket 上的本地短记忆：重连、新服务器实例、设备隔离。

LLM 是进程内假实现，不发网络请求。记忆文件落在临时目录。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

import websockets

import config.settings as settings_mod
from core.providers.memory.mem_local_short.mem_local_short import MemoryProvider
from core.utils.cache.config import CacheType
from core.utils.cache.manager import cache_manager
from core.websocket_server import WebSocketServer

PROJECT = Path(__file__).resolve().parent.parent
FACT_A = "测试机器人喜欢蓝色纸风车"
FACT_B = "另一个设备喜欢红色风铃"
RECALL = "请只根据记忆说出我的喜好"


class RecordingLLM:
    """聊天与记忆总结共用。总结请求返回 JSON，聊天请求记下完整 dialogue。"""

    def __init__(self):
        self.model_name = "deepseek-flash"
        self.api_key = "sk-test-not-real"
        self.chat_prompts = []
        self.chat_event = threading.Event()
        self._lock = threading.Lock()

    def response(self, session_id, dialogue, **kwargs):
        blob = json.dumps(dialogue, ensure_ascii=False)
        if "时空记忆编织者" in blob:
            quotes = []
            if FACT_A in blob:
                quotes.append(FACT_A)
            if FACT_B in blob:
                quotes.append(FACT_B)
            yield json.dumps({"高光语录": quotes}, ensure_ascii=False)
            return
        with self._lock:
            self.chat_prompts.append(dialogue)
            self.chat_event.set()
        yield "好的。"

    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        dialogue = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return "".join(self.response("", dialogue, **kwargs))


class _StubTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True

    async def open_audio_channels(self, conn):
        return None

    def store_tts_text(self, sentence_id, text):
        return None


class _StubASR:
    async def open_audio_channels(self, conn):
        return None


def _config(port: int, memory_file: str) -> dict:
    return {
        "server": {
            "ip": "127.0.0.1",
            "port": port,
            "http_port": 0,
            "auth_key": "test-auth-key-not-a-secret",
            "auth": {"enabled": False, "allowed_devices": []},
            "websocket": f"ws://127.0.0.1:{port}/xiaozhi/v1/",
            "vision_explain": "http://127.0.0.1/mcp/vision/explain",
            "timezone_offset": 8,
        },
        "log": {
            "log_level": "WARNING",
            "log_dir": str(PROJECT / "tmp"),
            "log_file": "pytest-memory.log",
            "data_dir": str(PROJECT / "data"),
            "log_format": "{message}",
            "log_format_file": "{message}",
        },
        "selected_module": {
            "VAD": "SileroVAD",
            "ASR": "StubASR",
            "LLM": "MockLLM",
            "TTS": "StubTTS",
            "Memory": "mem_local_short",
            "Intent": "nointent",
            "VLLM": "noop",
        },
        "prompt": "你是测试机器人。",
        "prompt_template": str(PROJECT / "agent-base-prompt.txt"),
        "exit_commands": ["退出"],
        "wakeup_words": [],
        "enable_wakeup_words_response_cache": False,
        "close_connection_no_voice_time": 600,
        "delete_audio": True,
        "memory_init_timeout": 30,
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
        "Memory": {
            "mem_local_short": {
                "type": "mem_local_short",
                "llm": "MockLLM",
                "file_path": memory_file,
            }
        },
        "LLM": {
            "MockLLM": {
                "type": "openai",
                "model_name": "deepseek-flash",
                "url": "https://api.deepseek.com",
                "api_key": "sk-test-not-real",
                "thinking": "disabled",
            }
        },
        "Intent": {"nointent": {"type": "nointent"}},
        "read_config_from_api": False,
    }


def _memory_block(dialogue) -> str:
    system = next(item["content"] for item in dialogue if item["role"] == "system")
    match = re.search(r"<memory>(.*?)</memory>", system, flags=re.DOTALL)
    return match.group(1) if match else ""


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def _wait_until_accepting(port: int):
    def _port_open() -> bool:
        probe = __import__("socket").socket()
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
            return
        await asyncio.to_thread(time.sleep, 0.05)
    raise RuntimeError(f"websocket server did not bind 127.0.0.1:{port}")


async def _start_server(memory_file: str, llm: RecordingLLM):
    port = _free_port()
    config = _config(port, memory_file)
    settings_mod.config_file_valid = True
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    server = WebSocketServer(config)
    task = asyncio.create_task(server.start())
    await _wait_until_accepting(port)
    return port, task


async def _stop_server(task: asyncio.Task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _speak(port: int, device_id: str, client_id: str, text: str, llm: RecordingLLM):
    before = len(llm.chat_prompts)
    llm.chat_event.clear()
    uri = f"ws://127.0.0.1:{port}/xiaozhi/v1/"
    headers = {"Device-Id": device_id, "Client-Id": client_id}
    async with websockets.connect(
        uri,
        additional_headers=headers,
        ping_interval=None,
        open_timeout=5,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "features": {"mcp": False},
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        hello = await asyncio.wait_for(ws.recv(), timeout=5)
        assert "session_id" in hello
        # hello 在组件初始化完成前就返回。立刻发文本，用来卡住
        # “记忆还没按 device-id 加载完就开始对话”的时序。
        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))
        ok = await asyncio.to_thread(llm.chat_event.wait, 20)
        assert ok, "mock LLM was not called"
        assert len(llm.chat_prompts) > before
        dialogue = llm.chat_prompts[-1]
    return dialogue


def _wait_for_fact(path: str, fact: str, timeout: float = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and fact in Path(path).read_text(encoding="utf-8"):
            return True
        time.sleep(0.05)
    return False


def test_ws_reconnect_restart_and_device_isolation(tmp_path, monkeypatch):
    """同一 Device-Id 重连和新服务器实例都能在 <memory> 里看到摘要；另一台设备看不到。"""
    monkeypatch.chdir(PROJECT)
    memory_file = str(tmp_path / ".memory.yaml")
    llm = RecordingLLM()
    original_init = MemoryProvider.init_memory

    def slow_init(self, *args, **kwargs):
        # 长于 hello 之后立刻发文本的间隔。不先等 memory_ready 的话，
        # 这轮对话会读到空摘要。
        time.sleep(0.8)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(MemoryProvider, "init_memory", slow_init)

    import core.websocket_server as ws_mod
    import core.connection as conn_mod
    import core.utils.llm as llm_mod
    import core.utils.prompt_manager as prompt_mod

    monkeypatch.setattr(
        ws_mod,
        "initialize_modules",
        lambda *args, **kwargs: {
            "llm": llm,
            "vad": object(),
            "asr": _StubASR(),
            "intent": None,
            "memory": None,
        },
    )
    monkeypatch.setattr(conn_mod, "initialize_tts", lambda config: _StubTTS())
    monkeypatch.setattr(conn_mod, "initialize_asr", lambda config: _StubASR())
    monkeypatch.setattr(llm_mod, "create_instance", lambda *args, **kwargs: llm)
    monkeypatch.setattr(
        prompt_mod.PromptManager, "_get_location_info", lambda self, ip: "测试市"
    )
    monkeypatch.setattr(
        prompt_mod.PromptManager,
        "_get_weather_info",
        lambda self, conn, location: "晴",
    )

    async def _run():
        port, task = await _start_server(memory_file, llm)
        try:
            first = await _speak(port, "aa:bb:cc:dd:ee:01", "client-a", FACT_A, llm)
            assert FACT_A not in _memory_block(first)
            # 同一进程里断开再连。文件里已经有摘要之后才问。
            assert await asyncio.to_thread(_wait_for_fact, memory_file, FACT_A)
            recalled = await _speak(port, "aa:bb:cc:dd:ee:01", "client-a", RECALL, llm)
            block = _memory_block(recalled)
            assert FACT_A in block
            assert FACT_B not in block

            await _speak(port, "aa:bb:cc:dd:ee:02", "client-b", FACT_B, llm)
            assert await asyncio.to_thread(_wait_for_fact, memory_file, FACT_B)

            recalled_b = await _speak(port, "aa:bb:cc:dd:ee:02", "client-b", RECALL, llm)
            block_b = _memory_block(recalled_b)
            assert FACT_B in block_b
            assert FACT_A not in block_b

            recalled_a_again = await _speak(
                port, "aa:bb:cc:dd:ee:01", "client-a", RECALL, llm
            )
            block_a = _memory_block(recalled_a_again)
            assert FACT_A in block_a
            assert FACT_B not in block_a
        finally:
            await _stop_server(task)

        # 新的 WebSocketServer 实例，不复用上一个进程内的 MemoryProvider。
        port, task = await _start_server(memory_file, llm)
        try:
            restarted = await _speak(port, "aa:bb:cc:dd:ee:01", "client-a", RECALL, llm)
            block = _memory_block(restarted)
            assert FACT_A in block
            assert FACT_B not in block
            restarted_b = await _speak(port, "aa:bb:cc:dd:ee:02", "client-b", RECALL, llm)
            block_b = _memory_block(restarted_b)
            assert FACT_B in block_b
            assert FACT_A not in block_b
        finally:
            await _stop_server(task)

    previous_sleep = asyncio.sleep

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
    try:
        asyncio.run(_run())
    finally:
        asyncio.sleep = previous_sleep
