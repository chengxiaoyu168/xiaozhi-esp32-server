"""mem_local_short 身份隔离与文件往返。不访问真实 LLM。"""
from __future__ import annotations

import asyncio
import json
import os
import threading

from core.providers.memory.mem_local_short.mem_local_short import MemoryProvider


FACT_A = "测试机器人喜欢蓝色纸风车"
FACT_B = "另一个设备喜欢红色风铃"


class _SummaryLLM:
    model_name = "deepseek-flash"
    api_key = "sk-test-not-real"

    def response(self, session_id, dialogue, **kwargs):
        blob = json.dumps(dialogue, ensure_ascii=False)
        quotes = []
        if FACT_A in blob:
            quotes.append(FACT_A)
        if FACT_B in blob:
            quotes.append(FACT_B)
        yield json.dumps({"高光语录": quotes}, ensure_ascii=False)

    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        dialogue = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return "".join(self.response("", dialogue, **kwargs))


class _Msg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


def _provider(path: str) -> MemoryProvider:
    provider = MemoryProvider({"file_path": path}, None)
    return provider


def test_mac_identity_roundtrip_and_does_not_leak_across_roles(tmp_path):
    path = str(tmp_path / ".memory.yaml")
    device_a = "aa:bb:cc:dd:ee:01"
    device_b = "aa:bb:cc:dd:ee:02"
    llm = _SummaryLLM()

    first = _provider(path)
    first.init_memory(device_a, llm, summary_memory=None, save_to_file=True)
    first.short_memory = json.dumps({"高光语录": [FACT_A]}, ensure_ascii=False)
    first.save_memory_to_file()

    # 同一个实例改绑到没有记忆的设备时，必须丢掉上一台设备的摘要。
    first.init_memory(device_b, llm, summary_memory=None, save_to_file=True)
    assert FACT_A not in (first.short_memory or "")
    assert first.short_memory == ""

    reloaded = _provider(path)
    reloaded.init_memory(device_a, llm, summary_memory=None, save_to_file=True)
    assert FACT_A in reloaded.short_memory

    other = _provider(path)
    other.init_memory(device_b, llm, summary_memory=None, save_to_file=True)
    assert other.short_memory == ""


def test_save_memory_keeps_devices_isolated(tmp_path):
    path = str(tmp_path / ".memory.yaml")
    llm = _SummaryLLM()

    async def _save(role, fact):
        provider = _provider(path)
        provider.init_memory(role, llm, summary_memory=None, save_to_file=True)
        msgs = [_Msg("user", fact), _Msg("assistant", "好的")]
        saved = await provider.save_memory(msgs, session_id="s")
        assert saved and fact in saved

    asyncio.run(_save("11:22:33:44:55:01", FACT_A))
    asyncio.run(_save("11:22:33:44:55:02", FACT_B))

    again_a = _provider(path)
    again_a.init_memory("11:22:33:44:55:01", llm, None, True)
    again_b = _provider(path)
    again_b.init_memory("11:22:33:44:55:02", llm, None, True)
    assert FACT_A in again_a.short_memory
    assert FACT_B not in again_a.short_memory
    assert FACT_B in again_b.short_memory
    assert FACT_A not in again_b.short_memory


def test_concurrent_saves_do_not_drop_a_device(tmp_path):
    path = str(tmp_path / ".memory.yaml")
    llm = _SummaryLLM()
    errors = []

    def _worker(role, fact):
        try:
            provider = _provider(path)
            provider.init_memory(role, llm, None, True)
            msgs = [_Msg("user", fact), _Msg("assistant", "好的")]
            asyncio.run(provider.save_memory(msgs, "s"))
        except Exception as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=("dev-a", FACT_A)),
        threading.Thread(target=_worker, args=("dev-b", FACT_B)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    text = open(path, encoding="utf-8").read()
    assert FACT_A in text
    assert FACT_B in text
    assert os.path.isfile(path)
