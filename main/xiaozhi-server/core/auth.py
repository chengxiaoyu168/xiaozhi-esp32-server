import hmac
import base64
import hashlib
import os
import re
import time

_BOOTSTRAP_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AuthenticationError(Exception):
    """认证异常"""

    pass


def normalize_device_id(device_id) -> str:
    """Compare MAC / device ids without case or surrounding whitespace."""
    return str(device_id or "").strip().lower()


def device_allowed(device_id, allowed_devices, auth_enabled: bool) -> bool:
    """Gate OTA token issuance and websocket admission.

    Auth disabled: every device is accepted (single-module open mode).
    Auth enabled: fail closed. Only ids listed in ``allowed_devices`` pass.
    An empty allowlist allows nobody. Matching is case-insensitive; the
    token itself is still signed with the device's original header value.
    """
    if not auth_enabled:
        return True
    allowed = set()
    for item in allowed_devices or []:
        normalized = normalize_device_id(item)
        if normalized:
            allowed.add(normalized)
    if not allowed:
        return False
    return normalize_device_id(device_id) in allowed


def load_bootstrap_secret(path: str) -> str:
    """Read the local OTA path credential. Empty string if missing or weak."""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return ""
    if not _BOOTSTRAP_RE.fullmatch(line):
        return ""
    return line


def bootstrap_matches(presented: str, expected: str) -> bool:
    """Constant-time compare. Wrong length still touches the expected secret."""
    if not expected or not _BOOTSTRAP_RE.fullmatch(expected):
        return False
    presented_b = (presented or "").encode("utf-8")
    expected_b = expected.encode("utf-8")
    if len(presented_b) != len(expected_b):
        hmac.compare_digest(expected_b, expected_b)
        return False
    return hmac.compare_digest(presented_b, expected_b)


def load_public_websocket_url(path: str) -> str:
    """Current wss/ws endpoint written by the local tunnel helper. Not a secret."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return ""
    if not (line.startswith("ws://") or line.startswith("wss://")):
        return ""
    if any(char in line for char in (" ", "\r", "\n", "?")):
        return ""
    if "/xiaozhi/ota/" in line:
        return ""
    return line


class AuthManager:
    """
    统一授权认证管理器
    生成与验证 client_id device_id token（HMAC-SHA256）认证三元组
    token 中不含明文 client_id/device_id，只携带签名 + 时间戳; client_id/device_id在连接时传递
    在 MQTT 中 client_id: client_id, username: device_id, password: token
    在 Websocket 中，header:{Device-ID: device_id, Client-ID: client_id, Authorization: Bearer token, ......}
    """

    def __init__(
        self,
        secret_key: str,
        expire_seconds: int = 60 * 60 * 24 * 30,
        token_epoch: int = 0,
    ):
        if not expire_seconds or expire_seconds < 0:
            self.expire_seconds = 60 * 60 * 24 * 30
        else:
            self.expire_seconds = expire_seconds
        self.secret_key = secret_key
        try:
            self.token_epoch = int(token_epoch or 0)
        except (TypeError, ValueError):
            self.token_epoch = 0

    def _token_content(self, client_id: str, username: str, ts: int) -> str:
        content = f"{client_id}|{username}|{ts}"
        if self.token_epoch:
            content = f"{content}|{self.token_epoch}"
        return content

    def _sign(self, content: str) -> str:
        """HMAC-SHA256签名并Base64编码"""
        sig = hmac.new(
            self.secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """
        生成 token
        Args:
            client_id: 设备连接ID
            username: 设备用户名（通常为deviceId）
        Returns:
            str: token字符串
        """
        ts = int(time.time())
        content = self._token_content(client_id, username, ts)
        signature = self._sign(content)
        # token仅包含签名与时间戳，不包含明文信息
        token = f"{signature}.{ts}"
        return token

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """
        验证token有效性
        Args:
            token: 客户端传入的token
            client_id: 连接使用的client_id
            username: 连接使用的username
        """
        try:
            sig_part, ts_str = token.split(".")
            ts = int(ts_str)
            if int(time.time()) - ts > self.expire_seconds:
                return False  # 过期

            expected_sig = self._sign(self._token_content(client_id, username, ts))
            if not hmac.compare_digest(sig_part, expected_sig):
                return False

            return True
        except Exception:
            return False
