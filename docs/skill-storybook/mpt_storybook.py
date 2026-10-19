#!/usr/bin/env python3
"""
操作已部署的 MoneyPrinterTurbo 手绘故事动画（storybook）服务。

只用标准库，通过 HTTP API 提交任务、等待完成、生成预览拼图：
  python3 mpt_storybook.py run --subject "主题" --script-file script.txt --genre explainer
  python3 mpt_storybook.py submit ...      # 只提交，打印 task_id
  python3 mpt_storybook.py wait TASK_ID    # 等待完成，打印成片网址
  python3 mpt_storybook.py status TASK_ID
  python3 mpt_storybook.py preview TASK_ID # 在容器内用 ffmpeg 生成 3x3 截图拼图

环境变量：
  MPT_API        API 地址，默认 http://127.0.0.1:8080
  MPT_PUBLIC     给用户看的网址前缀，默认同 MPT_API（例如 http://192.168.9.6:8080）
  MPT_REPO       仓库路径（storage 所在处），默认 ~/src/MoneyPrinterTurbo
  MPT_CONTAINER  API 容器名，默认 moneyprinterturbo-api
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.getenv("MPT_API", "http://127.0.0.1:8080").rstrip("/")
PUBLIC = os.getenv("MPT_PUBLIC", API).rstrip("/")
REPO = os.path.expanduser(os.getenv("MPT_REPO", "~/src/MoneyPrinterTurbo"))
CONTAINER = os.getenv("MPT_CONTAINER", "moneyprinterturbo-api")

STATE_COMPLETE, STATE_FAILED = 1, -1


def _request(method, path, payload=None, timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"status": exc.code, "message": body[:300]}


def _final_file(task_id):
    return os.path.join(REPO, "storage", "tasks", task_id, "final-1.mp4")


def build_payload(args):
    script = ""
    if args.script_file:
        with open(args.script_file, encoding="utf-8") as f:
            script = f.read().strip()
    elif args.script:
        script = args.script.strip()
    return {
        "video_subject": args.subject,
        "video_script": script,
        "video_script_prompt": args.prompt or "",
        "video_source": "storybook",
        "storybook_genre": args.genre,
        "storybook_style": args.style,
        "storybook_duration": args.duration,
        "storybook_use_doubao": args.doubao,
        "video_aspect": args.aspect,
        "video_language": args.language,
        "voice_name": args.voice,
        "bgm_type": "random" if args.bgm_volume > 0 else "",
        "bgm_volume": args.bgm_volume,
        "subtitle_enabled": not args.no_subtitles,
        # 默认不烧录，字幕另存 subtitle.srt，方便上传 YouTube 时另挂字幕。
        "subtitle_burn_in": args.burn_subtitles,
        # 浅色水彩底上白字看不清，默认加深色圆角底。
        "text_background_color": not args.no_subtitle_bg,
        "rounded_subtitle_background": not args.no_subtitle_bg,
        "max_file_size_mb": args.max_mb,
    }


def submit(args):
    payload = build_payload(args)
    if not payload["video_subject"]:
        sys.exit("--subject is required")
    result = _request("POST", "/api/v1/videos", payload)
    task_id = (result.get("data") or {}).get("task_id")
    if not task_id:
        sys.exit(f"submit failed: {result}")
    print(f"task_id: {task_id}", flush=True)
    return task_id


def status(task_id):
    result = _request("GET", f"/api/v1/tasks/{task_id}")
    return result.get("data") if result.get("status") == 200 else None


def wait(task_id, timeout_minutes=150, interval=30):
    """等待任务结束。API 重启会丢失内存里的任务状态，此时退回检查成片文件。"""
    deadline = time.time() + timeout_minutes * 60
    last = None
    missing = 0
    while time.time() < deadline:
        data = status(task_id)
        if data is None:
            missing += 1
            if os.path.isfile(_final_file(task_id)) and missing >= 2:
                print("task state is gone (API restarted?) but final-1.mp4 exists", flush=True)
                return _report_done(task_id, {})
            if missing >= 4:
                sys.exit(
                    "task not found (the API was probably restarted and in-memory task "
                    "state was lost); resubmit the task"
                )
        else:
            missing = 0
            state, progress = data.get("state"), data.get("progress")
            if (state, progress) != last:
                stage = _stage(progress)
                print(f"[{time.strftime('%H:%M:%S')}] progress {progress}% {stage}", flush=True)
                last = (state, progress)
            if state == STATE_COMPLETE:
                return _report_done(task_id, data)
            if state == STATE_FAILED:
                sys.exit(
                    f"FAILED at stage {data.get('failed_stage')}: {data.get('error')}\n"
                    f"logs: docker logs --since 30m {CONTAINER} | grep -E 'storybook|ERROR'"
                )
        time.sleep(interval)
    sys.exit(f"timed out after {timeout_minutes} minutes; check: python3 {sys.argv[0]} status {task_id}")


def _stage(progress):
    progress = progress or 0
    if progress < 15:
        return "(writing script)"
    if progress < 30:
        return "(voice)"
    if progress < 60:
        return "(Claude is drawing the animation, ~10-25 min)"
    if progress < 90:
        return "(recording frames)"
    if progress < 95:
        return "(compositing subtitles + BGM)"
    return "(compressing)"


def _report_done(task_id, data):
    path = _final_file(task_id)
    size = os.path.getsize(path) / 1048576 if os.path.isfile(path) else None
    info = {
        "task_id": task_id,
        "url": f"{PUBLIC}/tasks/{task_id}/final-1.mp4",
        "srt_url": f"{PUBLIC}/tasks/{task_id}/subtitle.srt",
        "srt_file": os.path.join(REPO, "storage", "tasks", task_id, "subtitle.srt"),
        "file": path,
        "size_mb": round(size, 2) if size else None,
        "duration_s": data.get("audio_duration"),
        "warnings": data.get("warnings"),
        "storyboard": os.path.join(REPO, "storage", "tasks", task_id, "storybook", "story.json"),
        "animation_page": os.path.join(REPO, "storage", "tasks", task_id, "storybook", "index.html"),
    }
    print("DONE " + json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    return info


def preview(task_id, columns=3, rows=3):
    """在 API 容器里用 ffmpeg 抽帧拼图（宿主机不一定有 ffmpeg），返回宿主机上的路径。"""
    duration = None
    data = status(task_id)
    if data:
        duration = data.get("audio_duration")
    count = columns * rows
    interval = max(1.0, float(duration or 120) / count)
    inside = f"/MoneyPrinterTurbo/storage/tasks/{task_id}"
    command = [
        "docker", "exec", CONTAINER, "ffmpeg", "-y", "-v", "error",
        "-i", f"{inside}/final-1.mp4",
        "-vf", f"fps=1/{interval:.2f},scale=640:-1,tile={columns}x{rows}",
        "-frames:v", "1", f"{inside}/contact.jpg",
    ]
    subprocess.run(command, check=True)
    path = os.path.join(REPO, "storage", "tasks", task_id, "contact.jpg")
    print(f"contact sheet: {path}", flush=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_submit_args(p):
        p.add_argument("--subject", required=True, help="video topic (shown in history)")
        p.add_argument("--script-file", help="narration script, one sentence per line (used verbatim)")
        p.add_argument("--script", help="narration script text (alternative to --script-file)")
        p.add_argument("--prompt", help="extra instructions for auto-written scripts")
        p.add_argument("--genre", choices=["explainer", "story"], default="explainer")
        p.add_argument("--style", choices=["watercolor", "ink_wash", "gongbi_scroll", "night_picture_book"], default="watercolor")
        p.add_argument("--duration", type=int, default=120, help="target seconds when the script is auto-written")
        p.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
        p.add_argument("--language", default="zh-TW")
        p.add_argument("--voice", default="zh-TW-HsiaoChenNeural-Female")
        p.add_argument("--doubao", action="store_true", help="use Doubao TTS (needs DOUBAO_TTS_API_KEY)")
        p.add_argument("--bgm-volume", type=float, default=0.12)
        p.add_argument("--max-mb", type=float, default=20, help="file size cap, 0 = no compression")
        p.add_argument("--no-subtitles", action="store_true")
        p.add_argument("--no-subtitle-bg", action="store_true")
        p.add_argument("--burn-subtitles", action="store_true", help="draw subtitles into the video instead of a separate SRT")

    add_submit_args(sub.add_parser("submit"))
    run_parser = sub.add_parser("run", help="submit and wait")
    add_submit_args(run_parser)
    run_parser.add_argument("--timeout", type=int, default=150, help="minutes")
    wait_parser = sub.add_parser("wait")
    wait_parser.add_argument("task_id")
    wait_parser.add_argument("--timeout", type=int, default=150, help="minutes")
    sub.add_parser("status").add_argument("task_id")
    sub.add_parser("preview").add_argument("task_id")

    args = parser.parse_args()
    if args.command == "submit":
        submit(args)
    elif args.command == "run":
        wait(submit(args), args.timeout)
    elif args.command == "wait":
        wait(args.task_id, args.timeout)
    elif args.command == "status":
        print(json.dumps(status(args.task_id), ensure_ascii=False, indent=2))
    elif args.command == "preview":
        preview(args.task_id)


if __name__ == "__main__":
    main()
