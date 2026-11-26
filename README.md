# openai-edge-tts（Edge TTS 7.3.1 补丁版）

一个 **OpenAI 兼容的文本转语音（TTS）服务**，底层使用 Microsoft Edge TTS 引擎。
本项目基于 [travisvn/openai-edge-tts](https://github.com/travisvn/openai-edge-tts) 修改而来，
保留了原项目的 OpenAI TTS API 设计，并针对镜像体积、微软服务 403、多模型暴露、
资源限制等问题做了补丁和精简。

## 项目简介

- 提供 OpenAI 风格的 `POST /v1/audio/speech` 接口，可直接替换 OpenAI TTS 的
  `base_url`。
- 合成引擎使用免费的 Microsoft Edge TTS，无需 Azure 订阅密钥。
- 支持 `mp3`、`wav`、`opus`、`aac`、`flac`、`pcm` 六种音频格式。
- 支持 SSE 流式输出，返回 OpenAI 风格的 `speech.audio.delta` / `speech.audio.done`。
- 内置音色列表接口，并兼容 OpenAI 音色别名（如 `alloy`、`nova`、`echo`）。
- 额外兼容 ElevenLabs 和 Azure TTS 的部分接口。
- 内置限流、排队、熔断、重试、临时文件清理等稳定性能力。
- 镜像精简：通过 `imageio-ffmpeg` 内置静态 FFmpeg，不安装体积较大的系统 FFmpeg。

## 与上游项目的关系

本项目是从 [travisvn/openai-edge-tts](https://github.com/travisvn/openai-edge-tts)
派生出的修改版。上游提供了核心的 OpenAI TTS 兼容服务，本项目在此基础上做了以下改动：

- 固定 `edge-tts==7.3.1`，解决 2026 年微软侧导致的 `403` 无法合成问题。
- 合并并精简 `Dockerfile`，使用清华 PyPI 镜像加速构建。
- 使用 `imageio-ffmpeg` 提供内置静态 FFmpeg，避免安装系统级 FFmpeg。
- `/v1/models` 返回标准 OpenAI 模型列表格式。
- 新增 `TTS_MODELS` 环境变量，可自定义对外暴露的模型 ID。
- 增加异步生成器清理，减少退出时的 aiohttp/asyncio 告警。
- 完善 `docker-compose.yml` 和 `.env`，加入中文注释。

## 目录结构

```text
edge-tts/
├── app/
│   ├── config.py
│   ├── handle_text.py
│   ├── ratelimit.py
│   ├── server.py
│   ├── tts_handler.py
│   └── utils.py
├── .dockerignore
├── .env
├── .gitignore
├── changelog/
│   └── 0.0.1.md
├── images/
├── docker-compose.yml
├── Dockerfile
├── LICENSE
└── README.md
```

## 环境要求

- Docker 24+（推荐）
- 或 Python 3.12+（本地开发）
- 服务器需要能访问微软 Edge TTS 服务

## 快速部署

### 方式一：本地源码构建镜像

在项目根目录执行：

```bash
# 1. 构建镜像
docker build -t edge-tts-openai:0.0.1 .

# 2. 修改配置文件
# 编辑 .env，至少修改 API_KEY 和 PORT

# 3. 启动
docker compose up -d

# 4. 检查健康状态
curl http://127.0.0.1:5050/health
```

### 方式二：加载已经打包好的镜像

如果已经导出了镜像归档文件，可以先加载再启动：

```bash
# 加载镜像
docker load -i edge-tts-openai-0.0.1.tar.gz

# 修改 .env 后启动
docker compose up -d
```

> 默认 Compose 使用的镜像名是 `edge-tts-openai:0.0.1`。

### 方式三：不使用 Docker，直接运行

```bash
cd edge-tts

python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install \
  flask==3.1.3 \
  gevent==26.9.0 \
  python-dotenv==1.2.4 \
  edge-tts==7.3.1 \
  emoji==2.16.0 \
  aiohttp==3.12.15 \
  aiohappyeyeballs==2.7.1 \
  aiosignal==1.4.0 \
  attrs==26.1.0 \
  frozenlist==1.8.0 \
  multidict==6.1.0 \
  yarl==1.18.3 \
  propcache==0.5.4 \
  idna==3.20 \
  imageio-ffmpeg==0.8.1

# 将 imageio-ffmpeg 的 FFmpeg 放入 PATH（Linux/macOS 示例）
python -c "import imageio_ffmpeg, os, shutil; shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(os.getcwd(), 'ffmpeg'))"
export PATH="$PWD:$PATH"

cd app
python server.py
```

非 Docker 运行时，服务默认监听 `0.0.0.0:5050`。

## 配置说明

所有配置都从同目录的 `.env` 文件读取，请直接修改该文件。

常用配置如下，完整注释见 `.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_KEY` | 无（必须修改） | 接口鉴权密钥，调用时使用 `Authorization: Bearer <API_KEY>` |
| `PORT` | `5050` | 对外暴露的宿主机端口，容器内部固定为 `5050` |
| `TTS_MODELS` | `tts-1` | `/v1/models` 暴露的模型 ID，逗号分隔；多个 ID 只是别名，实际使用同一个 Edge TTS 引擎 |
| `DEFAULT_VOICE` | `zh-CN-XiaoxiaoNeural` | 默认音色 |
| `DEFAULT_RESPONSE_FORMAT` | `mp3` | 默认输出格式：`mp3` / `wav` / `opus` / `aac` / `flac` / `pcm` |
| `DEFAULT_SPEED` | `1.0` | 默认语速，范围 `0.25` 到 `4.0` |
| `DEFAULT_LANGUAGE` | `zh-CN` | 音色列表默认语言 |
| `REQUIRE_API_KEY` | `True` | 是否强制鉴权 |
| `EXPAND_API` | `True` | 是否启用 ElevenLabs / Azure 兼容端点 |
| `TTS_RETRY_ATTEMPTS` | `3` | 微软侧瞬态失败后的重试次数 |
| `TTS_RATE_LIMIT` | `1` | 每秒允许的合成请求数，`0` 表示不限速 |

## 使用方法

### 基础文本转语音

```bash
curl -X POST http://127.0.0.1:5050/v1/audio/speech \
  -H "Authorization: Bearer sk-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tts-1",
    "voice": "zh-CN-XiaoxiaoNeural",
    "input": "你好，世界",
    "response_format": "mp3"
  }' \
  -o output.mp3
```

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:5050/v1",
    api_key="sk-你的密钥",
)

response = client.audio.speech.create(
    model="tts-1",
    voice="zh-CN-XiaoxiaoNeural",
    input="你好，世界",
    response_format="wav",
)

response.write_to_file("output.wav")
```

### SSE 流式输出

在请求体中增加 `stream_format: "sse"`：

```bash
curl -N -X POST http://127.0.0.1:5050/v1/audio/speech \
  -H "Authorization: Bearer sk-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tts-1",
    "voice": "zh-CN-XiaoxiaoNeural",
    "input": "这是一段流式语音测试",
    "stream_format": "sse"
  }'
```

返回内容为：

```text
data: {"type":"speech.audio.delta","audio":"..."}
data: {"type":"speech.audio.done","usage":{...}}
```

## API 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health`、`/healthz` | 健康检查 |
| `POST` | `/v1/audio/speech`、`/audio/speech` | OpenAI TTS 合成 |
| `GET/POST` | `/v1/models`、`/models`、`/v1/audio/models`、`/audio/models` | 模型列表 |
| `GET` | `/v1/models/<id>`、`/models/<id>` | 单个模型信息 |
| `GET/POST` | `/v1/voices`、`/voices`、`/v1/voices/all`、`/voices/all` | 音色列表 |
| `GET/POST` | `/v1/audio/voices`、`/audio/voices` | 音色别名列表 |
| `POST` | `/elevenlabs/v1/text-to-speech/<voice_id>` | ElevenLabs 兼容 |
| `POST` | `/azure/cognitiveservices/v1` | Azure TTS 兼容 |

除 `/health` 和 `/healthz` 外，其余接口在 `REQUIRE_API_KEY=True` 时都需要鉴权。

## 兼容信息

### OpenAI 兼容

支持 `POST /v1/audio/speech`、`GET /v1/models` 等 OpenAI 风格接口。
可直接把 OpenAI SDK 的 `base_url` 指向 `http://<服务地址>:<端口>/v1`。

支持参数：

- `input`：要合成的文本，最多 4096 字符。
- `voice`：Edge 音色名或 OpenAI 别名。
- `model`：需在 `TTS_MODELS` 中，例如 `tts-1`。
- `response_format`：`mp3` / `wav` / `opus` / `aac` / `flac` / `pcm`。
- `speed`：`0.25` 到 `4.0`。
- `pitch`：扩展参数，支持 `+10Hz`、`-20Hz`、数字等，范围 `-100` 到 `+100`。
- `stream_format`：`audio`（默认）或 `sse`。

> 本服务只提供“文字转语音”。如果目标工具调用 `/v1/audio/transcriptions`
> 或 `/v1/audio/translations`（语音转文字），本服务不提供这两个能力。

### ElevenLabs / Azure 兼容

当 `EXPAND_API=True` 时，还提供：

- `POST /elevenlabs/v1/text-to-speech/<voice_id>`
- `POST /azure/cognitiveservices/v1`

这两个兼容接口为 Beta 能力，默认输出 `mp3`。

### 音色兼容

除直接使用 Edge 音色名外，还支持 OpenAI 风格别名：

```text
alloy、ash、ballad、coral、echo、fable、nova、onyx、sage、shimmer、verse
```

可通过以下接口查询音色：

```bash
curl "http://127.0.0.1:5050/v1/voices?language=zh-CN" \
  -H "Authorization: Bearer sk-你的密钥"
```

## Python 依赖说明

项目依赖已直接写进 `Dockerfile` 的安装命令，构建镜像时会自动安装。
主要依赖如下：

| 包 | 说明 |
| --- | --- |
| `flask` | HTTP 应用框架 |
| `gevent` | 高性能 WSGI 服务 |
| `python-dotenv` | 读取 `.env` 环境变量 |
| `edge-tts==7.3.1` | Edge TTS 客户端，固定版本以解决微软 403 |
| `emoji` | 文本清洗时的 emoji 处理 |
| `aiohttp` 及关联包 | Edge TTS 所需的异步依赖 |
| `imageio-ffmpeg==0.8.1` | 内置静态 FFmpeg，替代系统级 FFmpeg，减小镜像体积 |

注意事项：

- 不建议随意升级 `edge-tts`，除非重新验证微软侧合成稳定性。
- Docker 构建时会把 `imageio-ffmpeg` 提供的 FFmpeg 软链到 `/usr/local/bin/ffmpeg`。
- 非 Docker 运行时也需要让 `ffmpeg` 出现在 `PATH` 中，否则只能输出 `mp3`。

## 已验证项

本地测试已覆盖：

- `GET /health`：返回 `status=ok`、`edge_tts=7.3.1`、`ffmpeg=true`
- `GET /v1/models`：返回标准 OpenAI 模型列表格式
- `POST /v1/audio/speech`：`mp3` / `wav` / `opus` / `aac` / `flac` / `pcm` 六种格式
- `stream_format=sse`：流式输出 `speech.audio.delta` 和 `speech.audio.done`
- API Key 鉴权：缺失或错误密钥返回 `401`
- `GET /v1/voices`：音色列表查询

## 常见问题

### 为什么提示 `ffmpeg: not installed`？

只有 `mp3` 不依赖 FFmpeg。构建本项目的 Docker 镜像后会自动内置 FFmpeg；
如果本地直接运行，需要手动安装 FFmpeg 或把 `imageio-ffmpeg` 的 FFmpeg 加入 `PATH`。

### 为什么偶尔出现 `Task was destroyed but it is pending!`？

这是 Edge TTS 内部 aiohttp 连接在进程退出或回收阶段产生的清理提示，不是请求失败。
当前版本已增加异步生成器清理；如果仍偶发出现，且前面请求返回 `200`，可以忽略。

## License

本项目沿用上游 [travisvn/openai-edge-tts](https://github.com/travisvn/openai-edge-tts)
的许可证，详见 `LICENSE` 文件。
