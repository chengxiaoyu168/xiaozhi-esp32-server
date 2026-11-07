# 本机部署：DeepSeek 短记忆、声纹、受保护的 OTA

这份说明只覆盖本仓库里的改动，给已经在本机跑 `xiaozhi-server`（8000/8003）和 Caddy（127.0.0.1:18766）的环境使用。不要把服务绑到公网，不要把密钥写进仓库，也不要动宿主机上已经存在的 Docker API。

上游语音服务是 `xinnan-tech/voiceprint-api` `9ffadb1854900b2f7fa7e78978a363457083754f`。声纹改动在 `deploy/voiceprint-api/`，和 `main/xiaozhi-server/` 是两套代码。

## 记忆为什么会“写成功但想不起来”

`data/.memory.yaml` 按 WebSocket 的 `Device-Id` 存摘要，MAC 这种带冒号的 id 在 PyYAML 6 里可以正常往返。查不回来有两个代码问题：

1. 服务端在 hello 之后、系统提示词套上 `agent-base-prompt.txt` 之前，用的是不带 `<memory>` 的快速人设。摘要只会被填进已有的 `<memory>` 标签。设备一连上就说话时，模型看不到已经加载的摘要。重启后每一轮连接都会再遇到这个窗口。
2. 所有连接共用一个记忆对象。`load_memory` 在新 device-id 没有记录时不清空 `short_memory`，后一台设备会看到前一台的摘要，保存时还可能写错 key。

现在每个连接自己加载自己的 `Device-Id`。快速人设里没有 `<memory>` 时，非空摘要会被追加进去。对话会等到这次加载结束再进入模型。文件读写按路径加锁。摘要 JSON 不合法时不再打 `Save memory successful`。

`mem_local_short.llm` 继续指向 `DeepSeekLLM` 即可。`api.deepseek.com` 会自动带 `thinking: {type: disabled}`；配置里写 `thinking: disabled` 也是同一条路径。模型名用已经验证过的 `deepseek-flash`。

## 服务端配置

在 `main/xiaozhi-server/data/.config.yaml` 覆盖，不要改仓库里的 `config.yaml`，也不要把真实 key 提交上去。`server.auth_key` 必须固定。`app.py` 在它为空时会每次启动随机生成，已发出的 token 会全部失效。

```yaml
server:
  ip: 127.0.0.1
  port: 8000
  http_port: 8003
  websocket: ws://127.0.0.1:18766/xiaozhi/v1/
  vision_explain: http://127.0.0.1:8003/mcp/vision/explain
  auth_key: 换成一段只放在本机的长随机串
  auth:
    enabled: true
    # 固件 2.5.2 只在开机 OTA 时刷新 token，运行中重连沿用 NVS 里的旧 token。
    # 不填则 30 天。要盖住设备两次重启之间的最长开机时间。
    expire_seconds: 2592000
    allowed_devices:
      - "设备MAC，和固件 Device-Id 一致"

selected_module:
  LLM: DeepSeekLLM
  Memory: mem_local_short

LLM:
  DeepSeekLLM:
    type: openai
    model_name: deepseek-flash
    url: https://api.deepseek.com
    api_key: 你的deepseek web key
    thinking: disabled

Memory:
  mem_local_short:
    type: mem_local_short
    llm: DeepSeekLLM
```

允许列表为空且 `auth.enabled: true` 时，OTA 和 WebSocket 都拒绝。列表比较忽略大小写，token 仍按设备送来的原始 `Device-Id` / `Client-Id` 签名。

开启认证之后：

- 只有列表中的设备能从 `POST /xiaozhi/ota/` 拿到 `websocket.token`。其他设备是 HTTP 403，响应里没有 token。
- WebSocket 必须带 `Authorization: Bearer <token>`。在列表里但没有 token、token 错误、token 过期、或不在列表里，都会收到 `认证失败` 并断开。
- 固件 2.5.2 在 token 不含空格时自己加 `Bearer `，所以 OTA 返回的是不带前缀的 token。
- 设备每次开机先打 OTA，再把返回的 `url` 和 `token` 写入 NVS，然后才连 WebSocket。这就是续期路径。服务端不把 API key 下发给设备。
- 同一把 `auth_key` 重启后，未过期的 token 仍然有效。换了 key，要等设备下次开机重新 OTA。

`auth.enabled: false` 时行为与原来的单模块开放模式相同，不签发 token。

## Caddy

`deploy/caddy/Caddyfile` 在 `127.0.0.1:18766` 上同时反代：

- `/xiaozhi/v1/*` → `127.0.0.1:8000`
- `/xiaozhi/ota*` → `127.0.0.1:8003`

固件 OTA 地址填 `http://127.0.0.1:18766/xiaozhi/ota/`。设备如果不在这台机器上，父机需要自行把 Caddy 改到局域网地址；不要改成对公网开放。OTA 没有设备私钥，知道允许列表里的 MAC 就能换 token。

加载配置后重载 Caddy。不要动 Docker 的 `0.0.0.0:2375`。

## 声纹

官方 `voiceprint-api:latest`（ModelScope 1.13.0）的下载器在 `Content-Length` 为空时把已经下到的 `configuration.json`（约 392 字节）判为不完整，然后一直重试。新版 SDK 下好的约 29MB snapshot（`iic/speech_campplus_sv_zh-cn_3dspeaker_16k`，修订 `v1.0.0`）即使用户拷进旧缓存，旧的 `ModelFileSystemCache` 仍会重新下载。

ModelScope 1.13 本身支持本地目录：目录存在且含有 `configuration.json` 时，`pipeline()` 不会调用 `snapshot_download`。`deploy/voiceprint-api` 只走这条路径。权重不到 1MB，或只有 `configuration.json`，会直接退出，不会再打下载器。

在父机上：

```bash
# 下面这个目录必须同时有 configuration.json 和约 29MB 的权重文件。
# 新版缓存一般是：
#   .../models/iic/speech_campplus_sv_zh-cn_3dspeaker_16k/snapshots/v1.0.0
export VOICEPRINT_MODEL_DIR=/实际的/snapshots/v1.0.0

cd deploy/voiceprint-api
cp voiceprint.yaml data/.voiceprint.yaml
# 编辑 data/.voiceprint.yaml：
#   server.host: 127.0.0.1
#   server.authorization: 一段不少于 32 位、只放在本机的随机串
#   mysql 指向已有的私有 MySQL，不要改那台数据库的监听地址
#   库和表仍用上游 README 里的 voiceprint_db / voiceprints

docker compose build
docker compose up -d
curl -sS "http://127.0.0.1:8005/voiceprint/health?key=上面的authorization"
```

健康检查应返回 `{"total_voiceprints":0,"status":"healthy"}`。错误 key 是 401。这一步通过之前，不要在 `xiaozhi-server` 里启用声纹。

本环境没有加载 29MB 权重，也没有向数据库写入说话人。注册必须用真人的 wav，在父机上做：

```bash
curl -sS -X POST "http://127.0.0.1:8005/voiceprint/register" \
  -H "Authorization: Bearer 上面的authorization" \
  -F "speaker_id=owner" \
  -F "file=@/path/to/owner.wav"
```

成功后再把 xiaozhi 的声纹段写成：

```yaml
voiceprint:
  url: http://127.0.0.1:8005/voiceprint/health?key=上面的authorization
  speakers:
    - "owner,名字,一句给模型看的描述"
  similarity_threshold: 0.4
```

`url` 里的 `key` 要和 Bearer 令牌相同。`speakers` 为空时服务端会关掉声纹。识别接口是 `POST /voiceprint/identify`，由 xiaozhi-server 带 Bearer 调用，设备里不放这个令牌。

## 本仓库里怎么验证

在 `main/xiaozhi-server`：

```bash
python -m pytest \
  tests/core/providers/memory/test_mem_local_short.py \
  tests/core/utils/test_dialogue.py \
  tests/test_memory_ws_persistence.py \
  tests/test_ota_allowlist.py -q
```

在 `deploy/voiceprint-api`：

```bash
python -m pytest tests -q
```

WebSocket 用例使用假 LLM，断言 `<memory>` 里的摘要，而不是模型会不会自己说出来。OTA 用例使用测试 key。声纹用例断言本地目录被交给 `pipeline()`，以及 health/identify/register 的鉴权；register 只用错误令牌，不会写入身份。
