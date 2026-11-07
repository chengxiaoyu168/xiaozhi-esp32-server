"""健康检查和鉴权。模型与 MySQL 都是桩，不注册声纹，也不下载权重。"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_import_stubs(pipeline_calls: list):
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        get_device_name=lambda _idx: "cpu",
    )
    torch.is_tensor = lambda _value: False
    sys.modules["torch"] = torch

    pipelines = types.ModuleType("modelscope.pipelines")

    def pipeline(**kwargs):
        pipeline_calls.append(kwargs)
        return object()

    pipelines.pipeline = pipeline
    constant = types.ModuleType("modelscope.utils.constant")
    constant.Tasks = types.SimpleNamespace(speaker_verification="speaker-verification")
    modelscope = types.ModuleType("modelscope")
    modelscope.pipelines = pipelines
    utils = types.ModuleType("modelscope.utils")
    sys.modules["modelscope"] = modelscope
    sys.modules["modelscope.pipelines"] = pipelines
    sys.modules["modelscope.utils"] = utils
    sys.modules["modelscope.utils.constant"] = constant

    class _Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

        def close(self):
            return None

        rowcount = 0

    class _Connection:
        open = True

        def cursor(self):
            return _Cursor()

        def close(self):
            return None

        def rollback(self):
            return None

    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **_kwargs: _Connection()
    pymysql.Connection = _Connection
    sys.modules["pymysql"] = pymysql
    sys.modules["librosa"] = types.ModuleType("librosa")
    sys.modules["soundfile"] = types.ModuleType("soundfile")


def _load_app(tmp_path: Path, pipeline_calls: list):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "configuration.json").write_text('{"model_file": "campplus_cn_common.bin"}', encoding="utf-8")
    weight = snapshot / "campplus_cn_common.bin"
    with weight.open("wb") as handle:
        handle.truncate(1_000_000)

    data = tmp_path / "data"
    data.mkdir()
    token = "local-test-token-0123456789abcdef"
    (data / ".voiceprint.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8005",
                f"  authorization: {token}",
                "mysql:",
                "  host: 127.0.0.1",
                "  port: 3306",
                "  user: voiceprint",
                "  password: local-only",
                "  database: voiceprint_db",
                "voiceprint:",
                "  model_id: iic/speech_campplus_sv_zh-cn_3dspeaker_16k",
                "  model_revision: v1.0.0",
                f"  model_dir: {snapshot}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    _install_import_stubs(pipeline_calls)

    import os

    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        from app.application import app
    finally:
        os.chdir(previous)
    return app, token, snapshot


def test_health_accepts_configured_key_and_rejects_others(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    calls: list = []
    monkeypatch.delenv("VOICEPRINT_MODEL_DIR", raising=False)
    app, token, snapshot = _load_app(tmp_path, calls)
    assert calls, "pipeline() was not called"
    assert Path(calls[0]["model"]) == snapshot.resolve()
    assert calls[0]["model"] != "iic/speech_campplus_sv_zh-cn_3dspeaker_16k"

    with TestClient(app) as client:
        missing = client.get("/voiceprint/health")
        assert missing.status_code == 422

        rejected = client.get("/voiceprint/health", params={"key": "wrong-key-not-the-local-token"})
        assert rejected.status_code == 401

        healthy = client.get("/voiceprint/health", params={"key": token})
        assert healthy.status_code == 200
        body = healthy.json()
        assert body["status"] == "healthy"
        assert body["total_voiceprints"] == 0

        identify = client.post(
            "/voiceprint/identify",
            headers={"Authorization": "Bearer wrong-token"},
            data={"speaker_ids": "nobody"},
            files={"file": ("clip.wav", b"RIFF" + b"\x00" * 64, "audio/wav")},
        )
        assert identify.status_code == 401

        missing_auth = client.post("/voiceprint/identify")
        assert missing_auth.status_code in (401, 403)


def test_service_does_not_register_a_fabricated_speaker(tmp_path, monkeypatch):
    """本环境不把假身份写进声纹库。注册接口在错误令牌下必须失败。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    calls: list = []
    monkeypatch.delenv("VOICEPRINT_MODEL_DIR", raising=False)
    app, _token, _snapshot = _load_app(tmp_path, calls)
    with TestClient(app) as client:
        response = client.post(
            "/voiceprint/register",
            headers={"Authorization": "Bearer wrong-token"},
            data={"speaker_id": "fabricated-speaker"},
            files={"file": ("clip.wav", b"RIFF" + b"\x00" * 64, "audio/wav")},
        )
        assert response.status_code == 401
