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

允许列表为空且 `auth.enabled: true` 时，OTA 和 WebSocket 都拒绝。MAC 只是第二道限制，不能单独当凭证。

固件 2.5.2 把菜单里的 OTA URL 原样 POST 出去，不跟随重定向（`78/esp-ml307` HttpClient 3.7）。所以引导凭证必须就在这条 URL 的路径里，不能靠 302，也不能靠额外请求头。URL 要以 `/` 结尾。

```bash
python3 deploy/ota/generate-bootstrap.py
# 不打印凭证。只在本机读取 data/.ota-bootstrap，填进固件：
#   https://<tunnel-host>/xiaozhi/ota/<那个文件的一行>/
# 作废旧地址：python3 deploy/ota/generate-bootstrap.py --rotate
# 作废已经发出的 WebSocket token：把 data/.config.yaml 里 auth.token_epoch 加一后重启服务。
```

公网或反代上的 `POST /xiaozhi/ota/` 一律 404，不签发 token。路径凭证正确但 MAC 不在允许列表里是 403。凭证正确且 MAC 在列表里才返回当前的 `websocket.url` 和 `websocket.token`。WebSocket 仍要 `Authorization: Bearer <token>`，无效 token 拒绝。服务访问日志不要打开，路径会落到日志里。

`auth.enabled: false` 时仍是原来的本机开放模式，不检查路径凭证。

## Caddy 和临时隧道

`deploy/caddy/Caddyfile` 用站点地址和 `bind 127.0.0.1` 只听本机 `18766`，反代 `/xiaozhi/v1/*` 到 8000、`/xiaozhi/ota*` 到 8003，访问日志丢掉。文件里没有凭证。重载 Caddy。不要动 Docker 的 `0.0.0.0:2375`。

临时隧道脚本只负责把 cloudflared 的日志收成文件。它修不了网络。

```bash
sh deploy/tunnel/start-quick-tunnel.sh
# 只有日志出现 Registered tunnel connection 才写 data/.public-wss
# 只有主机名、没有这条登记日志时，写 data/.public-wss.unverified，OTA 不读它
```

`--loglevel error` 会丢掉主机名：cloudflared 用 Info 打 `https://….trycloudflare.com`。脚本固定 `--loglevel info`。父机上 info 日志已经能看到主机名，同时没有 Registered tunnel connection，公网请求 530。那是路径问题，不是解析问题。

本机 cloudflared 2026.10.7 没有 HTTP(S)_PROXY / ALL_PROXY。系统 DNS 对 Cloudflare 的 feature、API、region 名返回 198.18.0.1。`--protocol http2` 时 TCP 7844 能连上，TLS 立刻 EOF；QUIC 超时。脚本不改 DNS、不设代理、不自动换协议。需要时自己设 `CLOUDFLARED_PROTOCOL=auto|quic|http2`，这不会绕过已经挡住的 Cloudflare 路径。这台云代理没有复现父机网络。

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
