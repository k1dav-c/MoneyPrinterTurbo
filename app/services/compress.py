"""
按文件大小上限压缩成片，方便在社群平台传播（例如不超过 20MB）。

已经低于上限的文件原样保留。超出时按「目标大小 ÷ 时长」算出总码率，扣掉音频后
用 libx264 两遍编码精确控制体积；码率太低时先降分辨率，避免 1080p 被压出马赛克。
"""

import json
import os
import shutil
import subprocess
import tempfile

from loguru import logger

from app.utils import utils

AUDIO_BITRATE_KBPS = 96
# 封装开销和码率波动的安全余量。
SAFETY = 0.94
# 低于这个视频码率时，1080p 画面明显劣化，改为 720p；再低改为 540p。
_DOWNSCALE_STEPS = ((1200, 1080), (600, 720), (0, 540))
MIN_VIDEO_KBPS = 150


def _probe_duration(file_path: str) -> float:
    completed = subprocess.run(
        [utils.get_ffmpeg_binary(), "-hide_banner", "-i", file_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # ffmpeg -i 没有输出文件时以非 0 退出，时长在 stderr 的 Duration 行里。
    for line in completed.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            hours, minutes, seconds = line.split(",")[0].split()[1].split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise RuntimeError(f"cannot read duration of {file_path}")


def _probe_height(file_path: str) -> int:
    completed = subprocess.run(
        [utils.get_ffmpeg_binary(), "-hide_banner", "-i", file_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    for line in completed.stderr.splitlines():
        if "Video:" in line:
            for token in line.replace(",", " ").split():
                if "x" in token and token.split("x")[0].isdigit() and token.split("x")[-1].isdigit():
                    width, height = token.split("x")
                    return min(int(width), int(height))
    return 1080


def _encode(source: str, target: str, video_kbps: int, short_side: int, work_dir: str) -> None:
    ffmpeg = utils.get_ffmpeg_binary()
    passlog = os.path.join(work_dir, "pass")
    # 按短边缩放，横屏和竖屏都适用；-2 保证宽高为偶数。
    scale = (
        f"scale='if(gt(iw,ih),-2,{short_side})':'if(gt(iw,ih),{short_side},-2)'"
    )
    common = [
        "-vf", scale,
        "-c:v", "libx264", "-preset", "medium",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.5)}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-passlogfile", passlog,
    ]
    first = [ffmpeg, "-y", "-v", "error", "-i", source, *common, "-pass", "1", "-an", "-f", "mp4", os.devnull]
    second = [
        ffmpeg, "-y", "-v", "error", "-i", source, *common, "-pass", "2",
        "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k", "-movflags", "+faststart", target,
    ]
    for command in (first, second):
        completed = subprocess.run(command, capture_output=True, check=False, cwd=work_dir)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"ffmpeg compression failed: {detail}")


def plan(duration: float, max_bytes: int, source_short_side: int) -> tuple[int, int]:
    """返回 (视频码率 kbps, 输出短边像素)。"""
    total_kbps = max_bytes * 8 * SAFETY / 1000 / max(duration, 0.1)
    video_kbps = max(MIN_VIDEO_KBPS, int(total_kbps - AUDIO_BITRATE_KBPS))
    for threshold, short_side in _DOWNSCALE_STEPS:
        if video_kbps >= threshold:
            return video_kbps, min(short_side, source_short_side)
    return video_kbps, min(540, source_short_side)


def compress_to_limit(file_path: str, max_mb: float) -> dict | None:
    """
    把视频压到 ``max_mb`` 以内并原地替换，返回压缩信息；无需压缩时返回 None。

    压缩失败时保留原文件并抛出异常，由调用方决定是否只记为警告。
    """
    if not max_mb or max_mb <= 0 or not os.path.isfile(file_path):
        return None
    max_bytes = int(max_mb * 1024 * 1024)
    original_size = os.path.getsize(file_path)
    if original_size <= max_bytes:
        return None

    duration = _probe_duration(file_path)
    source_short_side = _probe_height(file_path)
    video_kbps, short_side = plan(duration, max_bytes, source_short_side)
    logger.info(
        f"compressing {file_path}: {original_size / 1048576:.1f}MB -> <= {max_mb}MB "
        f"(video {video_kbps}kbps, {short_side}p)"
    )
    work_dir = tempfile.mkdtemp(prefix="mpt-compress-", dir=os.path.dirname(file_path) or None)
    try:
        target = os.path.join(work_dir, "out.mp4")
        _encode(file_path, target, video_kbps, short_side, work_dir)
        # 两遍编码仍可能略超；按实际超出比例再降一次码率。
        if os.path.getsize(target) > max_bytes:
            ratio = max_bytes / os.path.getsize(target) * 0.97
            video_kbps = max(MIN_VIDEO_KBPS, int(video_kbps * ratio))
            _encode(file_path, target, video_kbps, short_side, work_dir)
        final_size = os.path.getsize(target)
        if final_size > max_bytes:
            logger.warning(
                f"compressed file is still {final_size / 1048576:.1f}MB "
                f"(limit {max_mb}MB); keeping the smallest result"
            )
        os.replace(target, file_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    info = {
        "original_mb": round(original_size / 1048576, 2),
        "final_mb": round(os.path.getsize(file_path) / 1048576, 2),
        "video_kbps": video_kbps,
        "short_side": short_side,
    }
    logger.success(f"compressed {file_path}: {json.dumps(info)}")
    return info
