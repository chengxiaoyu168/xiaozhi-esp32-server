"""本地 snapshot 解析。不导入 torch / modelscope，也不访问网络。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.model_resolver import (
    DEFAULT_MODEL_ID,
    ModelSnapshotError,
    is_complete_snapshot,
    resolve_speaker_model,
)


def _write_snapshot(directory: Path, weight_name: str = "campplus_cn_common.bin", size: int = 1_000_000):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "configuration.json").write_text(
        '{"model": {"type": "campplus-sv"}, "model_file": "campplus_cn_common.bin"}',
        encoding="utf-8",
    )
    weight = directory / weight_name
    with weight.open("wb") as handle:
        handle.truncate(size)
    return directory


def test_explicit_snapshot_directory_is_used(tmp_path):
    snapshot = _write_snapshot(tmp_path / "snapshots" / "v1.0.0")
    resolved = resolve_speaker_model(model_dir=str(snapshot), cache_roots=[])
    assert resolved == snapshot.resolve()


def test_repo_root_prefers_revision_v1_0_0(tmp_path):
    root = tmp_path / "models" / "iic" / "speech_campplus_sv_zh-cn_3dspeaker_16k"
    older = _write_snapshot(root / "snapshots" / "old")
    current = _write_snapshot(root / "snapshots" / "v1.0.0")
    # 让 old 的 mtime 更新，确认仍优先 v1.0.0 而不是“最新”。
    os.utime(older, (older.stat().st_atime, older.stat().st_mtime + 50))
    resolved = resolve_speaker_model(
        model_dir=str(root),
        revision="v1.0.0",
        cache_roots=[],
    )
    assert resolved == current.resolve()


def test_modern_cache_layout_is_found_without_model_dir(tmp_path):
    snapshot = _write_snapshot(
        tmp_path
        / "models"
        / "iic"
        / "speech_campplus_sv_zh-cn_3dspeaker_16k"
        / "snapshots"
        / "v1.0.0"
    )
    resolved = resolve_speaker_model(
        model_dir="",
        model_id=DEFAULT_MODEL_ID,
        cache_roots=[tmp_path],
    )
    assert resolved == snapshot.resolve()


def test_legacy_hub_layout_with_files_at_repo_root(tmp_path):
    snapshot = _write_snapshot(tmp_path / "hub" / "iic" / "speech_campplus_sv_zh-cn_3dspeaker_16k")
    resolved = resolve_speaker_model(model_dir="", cache_roots=[tmp_path])
    assert resolved == snapshot.resolve()


def test_configuration_json_alone_is_not_a_model(tmp_path):
    broken = tmp_path / "partial"
    broken.mkdir()
    config = broken / "configuration.json"
    config.write_text("{" + ("x" * 380) + "}", encoding="utf-8")
    assert config.stat().st_size < 1000
    assert not is_complete_snapshot(broken)
    with pytest.raises(ModelSnapshotError) as raised:
        resolve_speaker_model(model_dir=str(broken), cache_roots=[])
    message = str(raised.value)
    assert "392" in message
    assert "Content-Length" in message
    assert "VOICEPRINT_MODEL_DIR" in message


def test_tiny_weight_file_is_rejected(tmp_path):
    snapshot = _write_snapshot(tmp_path / "tiny", size=392)
    assert not is_complete_snapshot(snapshot)
    with pytest.raises(ModelSnapshotError):
        resolve_speaker_model(model_dir=str(snapshot), cache_roots=[])
