"""
豆包语音（火山引擎语音合成 2.0，seed-tts-2.0）客户端。

只用标准库发请求：接口是一次性 HTTP 流式返回，每行一个 JSON 块，``data``
字段是 base64 编码的 s16le PCM，按顺序拼接即可得到整句音频。同一个音色靠
``context_texts``（自然语言描述语气）和 ``post_process.pitch`` 区分旁白与角色。
"""

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
import wave
from typing import Any, Mapping

from loguru import logger

from app.config import config

DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"
SAMPLE_RATE = 24000
# 不足 0.25 秒的音频视为没有合成出来：接口偶尔会对空白文本返回成功码。
MIN_AUDIO_SECONDS = 0.25
_SUCCESS_CODES = {0, 20000000}


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """配置优先，其次是环境变量；密钥只在请求头里使用，不写日志。"""
    settings = config.app if settings is None else settings
    configured = str(settings.get("doubao_tts_api_key", "") or "").strip()
    return (
        configured
        or os.getenv("DOUBAO_TTS_API_KEY", "").strip()
        or os.getenv("DOUBAO_SPEECH_API_KEY", "").strip()
    )


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    return bool(get_api_key(settings))


def default_voice(settings: Mapping[str, Any] | None = None) -> str:
    settings = config.app if settings is None else settings
    return (
        str(settings.get("doubao_tts_voice", "") or "").strip()
        or os.getenv("DOUBAO_SPEECH_VOICE_TYPE", "").strip()
        or DEFAULT_VOICE
    )


def _resource_id(settings: Mapping[str, Any]) -> str:
    return (
        str(settings.get("doubao_tts_resource_id", "") or "").strip()
        or DEFAULT_RESOURCE_ID
    )


def _cache_key(payload: dict, resource_id: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True) + resource_id
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def synthesize(
    text: str,
    output_file: str,
    *,
    speaker: str = "",
    context: str = "",
    speech_rate: int = 0,
    pitch: int = 0,
    language: str = "",
    cache_dir: str = "",
    timeout: float = 60.0,
    settings: Mapping[str, Any] | None = None,
) -> float:
    """
    合成一句话并写成 24kHz 单声道 wav，返回音频秒数。

    ``cache_dir`` 非空时按请求内容缓存 PCM，重跑同一任务不会重复计费。
    """
    settings = config.app if settings is None else settings
    api_key = get_api_key(settings)
    if not api_key:
        raise ValueError(
            "Doubao TTS API key is not configured "
            "(set app.doubao_tts_api_key or DOUBAO_TTS_API_KEY)"
        )
    text = (text or "").strip()
    if not text:
        raise ValueError("Doubao TTS text is empty")

    additions: dict[str, Any] = {"disable_markdown_filter": True}
    if context:
        additions["context_texts"] = [context]
    if language:
        additions["explicit_language"] = language
    if pitch:
        additions["post_process"] = {"pitch": int(pitch)}
    resource_id = _resource_id(settings)
    payload = {
        "user": {"uid": "moneyprinterturbo-storybook"},
        "req_params": {
            "text": text,
            "speaker": speaker or default_voice(settings),
            "audio_params": {
                "format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "speech_rate": int(speech_rate),
            },
            # 接口要求 additions 是 JSON 字符串而不是对象。
            "additions": json.dumps(additions, ensure_ascii=False),
        },
    }

    cache_file = ""
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, _cache_key(payload, resource_id) + ".pcm")
    if cache_file and os.path.isfile(cache_file):
        with open(cache_file, "rb") as f:
            pcm = f.read()
    else:
        pcm = _request_pcm(payload, api_key, resource_id, timeout)
        if cache_file:
            with open(cache_file, "wb") as f:
                f.write(pcm)

    duration = len(pcm) / 2 / SAMPLE_RATE
    if duration < MIN_AUDIO_SECONDS:
        raise ValueError(f"Doubao TTS returned no usable audio for: {text[:40]}")
    with wave.open(output_file, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return duration


def _request_pcm(payload: dict, api_key: str, resource_id: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        str(config.app.get("doubao_tts_endpoint", "") or DEFAULT_ENDPOINT),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Doubao TTS HTTP {exc.code}: {detail}") from None

    chunks: list[bytes] = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            block = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = block.get("code", 0)
        if code not in _SUCCESS_CODES:
            raise RuntimeError(
                f"Doubao TTS error {code}: {str(block.get('message', ''))[:300]}"
            )
        data = block.get("data")
        if data:
            chunks.append(base64.b64decode(data))
    pcm = b"".join(chunks)
    # 兼容服务端用 RIFF 包装返回的情况：剥掉 44 字节 wav 头只留 PCM。
    if pcm[:4] == b"RIFF" and pcm[8:12] == b"WAVE":
        pcm = pcm[44:]
    if not pcm:
        logger.warning("Doubao TTS response contained no audio chunks")
    return pcm
