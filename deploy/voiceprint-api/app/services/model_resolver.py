"""Locate a complete local CampPlus snapshot for ModelScope 1.13.

ModelScope 1.13 ``http_get_file`` does::

    total = int(content_length) if content_length is not None else None
    if total != downloaded_length:
        raise FileDownloadError(...)

When the hub responds without ``Content-Length`` (the observed
``configuration.json`` body is 392 bytes and ``content_length`` is None),
``None != 392`` is always true, so the downloader deletes the file and
retries. A snapshot copied from a newer ModelScope SDK is also invisible
to 1.13's ``ModelFileSystemCache`` unless ``pipeline()`` is given the
snapshot directory itself. ``modelscope.pipelines.builder.normalize_model_input``
skips ``snapshot_download`` when that directory already exists and contains
``configuration.json``.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL_ID = "iic/speech_campplus_sv_zh-cn_3dspeaker_16k"
DEFAULT_REVISION = "v1.0.0"
WEIGHT_SUFFIXES = {".bin", ".pt", ".pth", ".onnx", ".safetensors", ".ckpt"}
# The published snapshot is about 29MB. A lone 392-byte configuration.json
# is the failed download, not a usable model.
MIN_WEIGHT_BYTES = 1_000_000


class ModelSnapshotError(RuntimeError):
    """Raised when no complete local snapshot is available."""


def _is_weight(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in WEIGHT_SUFFIXES
        and path.stat().st_size >= MIN_WEIGHT_BYTES
    )


def is_complete_snapshot(path: Path) -> bool:
    directory = Path(path)
    if not directory.is_dir():
        return False
    if not (directory / "configuration.json").is_file():
        return False
    return any(_is_weight(child) for child in directory.iterdir())


def _pick_snapshot(path: Path, revision: str) -> Path | None:
    if not path.exists():
        return None
    if is_complete_snapshot(path):
        return path.resolve()
    snapshots = path / "snapshots"
    if not snapshots.is_dir():
        return None
    preferred = snapshots / revision
    if is_complete_snapshot(preferred):
        return preferred.resolve()
    found = [child for child in snapshots.iterdir() if is_complete_snapshot(child)]
    if not found:
        return None
    found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return found[0].resolve()


def cache_candidates(model_id: str, cache_roots: list[Path] | None = None) -> list[Path]:
    org, _, name = model_id.partition("/")
    if not name:
        raise ModelSnapshotError(f"model id must look like org/name, got {model_id!r}")
    roots: list[Path] = []
    if cache_roots is not None:
        roots.extend(Path(root) for root in cache_roots)
    else:
        env_root = os.environ.get("MODELSCOPE_CACHE", "").strip()
        if env_root:
            roots.append(Path(env_root))
        roots.append(Path.home() / ".cache" / "modelscope")
    candidates: list[Path] = []
    for root in roots:
        # Newer SDK (snapshot hash layout) and ModelScope 1.13 hub layout.
        candidates.append(root / "models" / org / name)
        candidates.append(root / "hub" / "models" / org / name)
        candidates.append(root / "hub" / org / name)
        candidates.append(root / org / name)
    return candidates


def resolve_speaker_model(
    model_dir: str | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    revision: str = DEFAULT_REVISION,
    cache_roots: list[Path] | None = None,
) -> Path:
    """Return a local snapshot directory. Never calls the network."""
    explicit = (model_dir or "").strip()
    if not explicit and cache_roots is None:
        explicit = os.environ.get("VOICEPRINT_MODEL_DIR", "").strip()
    if explicit:
        search = [Path(explicit)]
    else:
        search = cache_candidates(model_id, cache_roots)

    checked: list[str] = []
    for candidate in search:
        checked.append(str(candidate))
        resolved = _pick_snapshot(candidate, revision or DEFAULT_REVISION)
        if resolved is not None:
            return resolved

    raise ModelSnapshotError(
        "没有找到可用的本地声纹模型，已停止下载以避免 ModelScope 1.13 "
        "在 configuration.json（约 392 字节、Content-Length 为空）上反复 "
        "FileDownloadError。请把已下好的 "
        f"{model_id} snapshot {revision or DEFAULT_REVISION} "
        "目录（里面要有 configuration.json 和约 29MB 权重文件）"
        "配到 voiceprint.model_dir 或环境变量 VOICEPRINT_MODEL_DIR。"
        "只拷贝 392 字节的 configuration.json，或把新版 cache 根目录交给旧下载器，"
        "都不会被当成完整模型。已检查: " + ", ".join(checked)
    )
