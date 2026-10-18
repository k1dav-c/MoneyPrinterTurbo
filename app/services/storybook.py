"""
手绘故事动画模式（video_source = "storybook"）。

流程参考 hand-drawn-storybook-animation skill（GPT 写剧本 → Claude 制作 →
豆包配音）：
  1. 当前 LLM 分三轮生成剧本：写故事 → 以挑剔观众和动画导演的角度批判并修改
     → 输出分镜 JSON（每个镜头的景别、画面、台词）；
  2. 逐句配音（配置了豆包语音就用豆包，否则复用 MPT 的 TTS），按真实时长排出
     时间线，并据此写字幕；
  3. 调用本机已登录的 claude CLI，带上 skill 内容，让它按分镜和时间线写出一个
     单文件 canvas 动画页面。页面必须暴露确定性的 ``STORY.renderFrame(t)``；
  4. 无头 Chromium 逐帧调用 renderFrame 截图，ffmpeg 编码成无声视频；
  5. 复用 ``video.generate_video`` 烧录字幕、混入背景音乐，产出 final-1.mp4。

页面里的配乐和字幕都不参与录制：配乐由 MPT 的 BGM 提供，字幕统一由 MPT 烧录，
这样字体、位置等设置和普通模式保持一致。
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoAspect, VideoParams
from app.services import doubao_tts, llm, task_artifacts, video, voice
from app.services import state as sm
from app.utils import utils

SOURCE_NAME = "storybook"

# 与 skill 的四种参考作品一一对应；值会原样写进给 Claude 的提示词。
STYLES = {
    "watercolor": "hand-drawn watercolor picture book: pen ink lines, layered watercolor washes, cream paper (skill section 3.1)",
    "ink_wash": "Chinese ink-wash myth: rice paper, ink-dominant with sparse ochre/vermilion/azurite accents (skill section 3.2)",
    "gongbi_scroll": "gongbi + ink-wash city handscroll on silk: oblique-projection architecture, line drawing then light color (skill section 3.3, Canvas2D only)",
    "night_picture_book": "cozy night picture book: warm lamp light vs cool moonlight, boiling-line characters (skill section 3.4)",
}
DEFAULT_STYLE = "watercolor"

DEFAULT_SKILL_PATH = os.path.join(
    "resource", "skills", "hand-drawn-storybook-animation", "SKILL.md"
)
DEFAULT_CLAUDE_TIMEOUT = 2400.0
DEFAULT_FPS = 24

# 时间线参数（秒）：句间气口与 skill 一致取 0.36s。
LEAD_IN = 1.2
LINE_GAP = 0.36
SCENE_TAIL = 0.8
MIN_SCENE = 3.5
OUTRO = 2.5

# 每秒能读完的字数，用来把目标时长换算成剧本长度。
_CJK_CHARS_PER_SECOND = 4.2
_WORDS_PER_SECOND = 2.4
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])\s*|(?<=[.])\s+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")


@dataclass
class TimedLine:
    scene: int
    speaker: str
    text: str
    start: float
    end: float
    audio_file: str = ""


@dataclass
class Timeline:
    duration: float
    scenes: list[dict] = field(default_factory=list)
    lines: list[TimedLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "scenes": self.scenes,
            "lines": [
                {
                    "scene": line.scene,
                    "speaker": line.speaker,
                    "text": line.text,
                    "start": round(line.start, 3),
                    "end": round(line.end, 3),
                }
                for line in self.lines
            ],
        }


# ---------------------------------------------------------------- 剧本


def _target_length_hint(language: str, duration: int) -> str:
    if language.startswith(("zh", "ja")) or not language:
        chars = int(duration * _CJK_CHARS_PER_SECOND * 0.75)
        return f"about {chars} Chinese/Japanese characters of spoken text in total (if the story language is CJK), or about {int(duration * _WORDS_PER_SECOND * 0.75)} words otherwise"
    return f"about {int(duration * _WORDS_PER_SECOND * 0.75)} words of spoken text in total"


def _ask_llm(prompt: str, app_config=None) -> str:
    response = (
        llm._generate_response(prompt=prompt)
        if app_config is None
        else llm._generate_response(prompt=prompt, app_config=app_config)
    )
    if not isinstance(response, str) or not response.strip():
        raise ValueError("LLM returned an empty response")
    if response.startswith("Error: "):
        raise ValueError(response)
    return response.strip()


def _parse_story_json(text: str) -> dict:
    text = llm._strip_code_fence(text)
    try:
        story = json.loads(text)
    except json.JSONDecodeError:
        story = llm._extract_embedded_json(text, "{")
    if not isinstance(story, dict):
        raise ValueError("storyboard is not a JSON object")
    scenes = story.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("storyboard has no scenes")
    cleaned = []
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        lines = []
        for line in scene.get("lines") or []:
            if isinstance(line, str):
                line = {"speaker": "narrator", "text": line}
            if not isinstance(line, dict):
                continue
            text_value = str(line.get("text") or "").strip()
            if text_value:
                lines.append({
                    "speaker": str(line.get("speaker") or "narrator").strip() or "narrator",
                    "text": text_value,
                })
        scene["id"] = index
        scene["lines"] = lines
        cleaned.append(scene)
    if not any(scene["lines"] for scene in cleaned):
        raise ValueError("storyboard has no spoken lines")
    story["scenes"] = cleaned
    story.setdefault("characters", [])
    return story


def generate_story(params: VideoParams, app_config=None) -> dict:
    """三轮生成剧本并返回分镜 JSON；用户自带文案时只做分镜这一轮。"""
    subject = (params.video_subject or "").strip()
    language = (params.video_language or "").strip()
    duration = int(params.storybook_duration or 60)
    style = STYLES.get(params.storybook_style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
    language_rule = (
        f"Write all spoken lines in language code '{language}'."
        if language
        else "Write all spoken lines in the same language as the subject."
    )
    extra = (params.video_script_prompt or "").strip()

    if (params.video_script or "").strip():
        revised = params.video_script.strip()
        logger.info("storybook: using the provided script, skipping write/critique rounds")
    else:
        logger.info("storybook: round 1/3 - writing the story")
        draft = _ask_llm(
            "我要设计一个创意手绘动画，请你设计一个剧本，要求治愈、有吸引力、有传播价值。\n"
            "I want to make a creative hand-drawn animation. Design a script that is "
            "heart-warming, gripping and shareable. Include story, scenes, character "
            "actions and lines.\n"
            f"Subject: {subject}\n"
            f"Visual style: {style}\n"
            f"Target length: {duration} seconds, {_target_length_hint(language, duration)}. "
            "The protagonist and a hook must appear in the first 5 seconds; there must be "
            "a setup, development, surprise, climax and resolution, with at least one "
            "striking visual transformation. Characters must be original.\n"
            f"{language_rule}\n{extra}",
            app_config,
        )
        logger.info("storybook: round 2/3 - critiquing and revising")
        revised = _ask_llm(
            "站在挑剔的观众和动画导演的角度，批判一下这个剧本，哪里平淡、哪里不合理、"
            "哪里不够让人想转发，然后给我改好的版本。\n"
            "From the point of view of a picky audience and an animation director, "
            "critique this script (what is flat, illogical, not shareable), then output "
            "ONLY the full improved script, keeping the same target length.\n"
            f"{language_rule}\n\n--- SCRIPT ---\n{draft}",
            app_config,
        )

    logger.info("storybook: round 3/3 - building the storyboard")
    storyboard = _ask_llm(
        "根据剧本生成分镜：每个镜头一格，标上景别，写上画面内容和台词。\n"
        "Turn this script into a storyboard. Output ONLY a JSON object, no markdown, "
        "with this exact shape:\n"
        '{"title": str, "logline": str, "palette": str,\n'
        ' "characters": [{"id": str, "name": str, "look": str, "voice": str}],\n'
        ' "scenes": [{"shot": "wide|medium|close-up|insert|low-angle", '
        '"visual": str, "action": str, "transition": str,\n'
        '   "lines": [{"speaker": "narrator" or a character id, "text": str}]}]}\n'
        f"Rules: {max(4, duration // 9)}-{max(6, duration // 5)} scenes; every scene "
        "has 0-3 short lines (one sentence each, no stage directions inside text); "
        "'look' describes a fixed design so the character stays consistent; 'voice' "
        "describes age and personality for a TTS voice (tiny creatures only make "
        "cute sounds); visual descriptions are concrete and drawable in the style: "
        f"{style}. Keep the total spoken length {_target_length_hint(language, duration)}.\n"
        f"{language_rule}\n\n--- SCRIPT ---\n{revised}",
        app_config,
    )
    story = _parse_story_json(storyboard)
    story["style"] = params.storybook_style or DEFAULT_STYLE
    story["script"] = revised
    return story


# ---------------------------------------------------------------- 配音与时间线


def _character_voice(story: dict, speaker: str) -> str:
    for character in story.get("characters") or []:
        if isinstance(character, dict) and character.get("id") == speaker:
            return str(character.get("voice") or character.get("name") or "")
    return ""


def _speak_line_doubao(story: dict, line: dict, output_file: str, cache_dir: str) -> float:
    speaker = line["speaker"]
    text = line["text"]
    is_english = not _CJK_RE.search(text)
    if speaker == "narrator":
        return doubao_tts.synthesize(
            text,
            output_file,
            context="用温柔的年轻女声讲睡前故事，语速慢一点，轻轻的，像在对一个孩子说话。",
            speech_rate=-10,
            language="en" if is_english else "",
            cache_dir=cache_dir,
        )
    description = _character_voice(story, speaker)
    is_creature = any(
        word in description.lower() for word in ("creature", "sound", "叫声", "小生物")
    )
    return doubao_tts.synthesize(
        text,
        output_file,
        context=(
            "tiny creature, only a soft, round, cute sound, not a sentence"
            if is_creature
            else f"a character in a picture book: {description or speaker}"
        ),
        pitch=8 if is_creature else 2,
        language="en" if is_english else "",
        cache_dir=cache_dir,
    )


_MIX_SAMPLE_RATE = 24000


def _decode_pcm(audio_file: str):
    """用 ffmpeg 把任意音频解码成 24kHz 单声道 int16，不依赖 ffprobe。"""
    import numpy as np

    completed = subprocess.run(
        [
            utils.get_ffmpeg_binary(), "-v", "error", "-i", audio_file,
            "-f", "s16le", "-ac", "1", "-ar", str(_MIX_SAMPLE_RATE), "-",
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"failed to decode {audio_file}: {detail}")
    return np.frombuffer(completed.stdout, dtype=np.int16)


def _speak_line_mpt(params: VideoParams, text: str, output_file: str) -> float:
    sub_maker = voice.tts(
        text=text,
        voice_name=voice.parse_voice_name(params.voice_name or ""),
        voice_rate=params.voice_rate,
        voice_file=output_file,
    )
    if sub_maker is None or not os.path.isfile(output_file):
        raise RuntimeError(f"TTS failed for line: {text[:40]}")
    return len(_decode_pcm(output_file)) / _MIX_SAMPLE_RATE


def synthesize_voice(
    task_id: str,
    params: VideoParams,
    story: dict,
    report: Callable[[float], None] | None = None,
) -> Timeline:
    """逐句配音，按真实时长排时间线：上一句超长时后面的镜头自动顺延。"""
    work_dir = utils.task_dir(os.path.join(task_id, "storybook", "voice"))
    os.makedirs(work_dir, exist_ok=True)
    use_doubao = bool(params.storybook_use_doubao) and doubao_tts.is_enabled()
    if params.storybook_use_doubao and not use_doubao:
        logger.warning("storybook: Doubao TTS is not configured, falling back to MPT voice")
    logger.info(f"storybook: voice provider = {'doubao' if use_doubao else params.voice_name}")

    all_lines = [
        (scene["id"], line) for scene in story["scenes"] for line in scene["lines"]
    ]
    timeline = Timeline(duration=0.0)
    cursor = LEAD_IN
    done = 0
    for scene in story["scenes"]:
        scene_start = cursor
        for line in scene["lines"]:
            suffix = ".wav" if use_doubao else ".mp3"
            audio_file = os.path.join(work_dir, f"line-{done + 1:03d}{suffix}")
            if use_doubao:
                seconds = _speak_line_doubao(
                    story, line, audio_file, os.path.join(work_dir, "cache")
                )
            else:
                seconds = _speak_line_mpt(params, line["text"], audio_file)
            timeline.lines.append(
                TimedLine(
                    scene=scene["id"],
                    speaker=line["speaker"],
                    text=line["text"],
                    start=cursor,
                    end=cursor + seconds,
                    audio_file=audio_file,
                )
            )
            cursor += seconds + LINE_GAP
            done += 1
            if report:
                report(done / max(1, len(all_lines)))
        scene_end = max(cursor - LINE_GAP + SCENE_TAIL, scene_start + MIN_SCENE)
        timeline.scenes.append({
            "id": scene["id"],
            "start": round(scene_start, 3),
            "end": round(scene_end, 3),
            "shot": scene.get("shot", ""),
        })
        cursor = scene_end
    timeline.duration = cursor + OUTRO
    if timeline.scenes:
        timeline.scenes[-1]["end"] = round(timeline.duration, 3)
    return timeline


def write_audio(timeline: Timeline, output_file: str) -> None:
    """把逐句音频按时间线铺到静音轨上；音量由 generate_video 统一处理。"""
    import numpy as np

    track = np.zeros(int((timeline.duration + 0.2) * _MIX_SAMPLE_RATE), dtype=np.int32)
    for line in timeline.lines:
        samples = _decode_pcm(line.audio_file)
        start = int(line.start * _MIX_SAMPLE_RATE)
        end = min(len(track), start + len(samples))
        track[start:end] += samples[: end - start]
    pcm = np.clip(track, -32768, 32767).astype(np.int16).tobytes()
    completed = subprocess.run(
        [
            utils.get_ffmpeg_binary(), "-y", "-v", "error",
            "-f", "s16le", "-ac", "1", "-ar", str(_MIX_SAMPLE_RATE), "-i", "-",
            "-c:a", "libmp3lame", "-b:a", "192k", output_file,
        ],
        input=pcm,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"failed to write narration track: {detail}")


def _format_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_subtitles(timeline: Timeline, output_file: str, max_chars: int = 26) -> None:
    """按真实音频起止写字幕；过长的句子按标点拆开，时长按字数分摊。"""
    cues = []
    for line in timeline.lines:
        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(line.text) if p.strip()]
        if len(line.text) <= max_chars or len(parts) <= 1:
            parts = [line.text]
        total = sum(len(p) for p in parts) or 1
        start = line.start
        for part in parts:
            end = start + (line.end - line.start) * len(part) / total
            cues.append((start, end, part))
            start = end
    with open(output_file, "w", encoding="utf-8") as f:
        for index, (start, end, text) in enumerate(cues, start=1):
            f.write(f"{index}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}\n\n")


# ---------------------------------------------------------------- Claude 制作动画


RENDER_CONTRACT = """
## Output contract for this run (overrides anything above that conflicts)
You are running headless inside a video pipeline. Build the animation as ONE self-contained
file `index.html` in the current working directory (use the Write tool; use Edit for fixes).
No external resources at all: no CDN, no images, no web fonts, no network.

The page MUST:
1. Draw everything on a single full-window <canvas> sized to window.innerWidth x window.innerHeight
   (times devicePixelRatio). Canvas2D only - do NOT use WebGL (frames are rendered with a software rasterizer).
2. Define `window.STORY = { duration, renderFrame(t), ready }` where
   - `duration` is the total length in seconds (use timeline.json's duration),
   - `renderFrame(t)` synchronously draws the complete frame for story time `t` seconds.
     It must be a PURE function of t: the same t always gives the same picture, and calls may come
     in any order. Never use Date.now / performance.now / unseeded Math.random while rendering;
     use a seeded PRNG and precompute/pre-integrate everything during init. Pre-render static
     layers to offscreen canvases so a 1080p frame renders in well under 100 ms.
   - `ready` is `true` or a Promise that resolves when init (offscreen layers etc.) has finished.
3. When the URL contains `?render=1`: render nothing in the DOM besides the canvas (no buttons,
   controls, subtitles or overlays), play no audio, and do NOT run a requestAnimationFrame loop -
   the recorder calls renderFrame(t) itself. Without `?render=1`, a simple autoplay preview loop
   (requestAnimationFrame calling renderFrame) is enough. Do not implement Web Audio music.
4. NEVER draw subtitles or spoken lines as text - they are burned in later. Short art text such as
   a title card or chapter card is fine.
5. Follow timeline.json exactly: each scene's [start, end] window, and use line start/end times
   for camera moves, character acting and mouth movement. The picture must fill the whole frame
   at the given resolution (no letterboxing, no white unpainted paper edges showing).

Work incrementally so that no single tool call is huge (keep every Write/Edit under ~500 lines):
1. Read story.json and timeline.json (once).
2. Write index.html with the engine only: canvas setup, seeded PRNG, easing, camera keyframes,
   brush/watercolor/line-reveal helpers, character drawing functions, transitions, the STORY
   object and a scene table whose entries are still empty stubs. Mark the stub area with
   `/*SCENES*/` comments.
3. Fill in the scenes one Edit per scene (or two scenes per Edit), in order.
4. Re-read the file once to check for syntax errors and that every moment of the duration renders.
Aim for roughly 1200-2500 lines in total; prefer reusable helpers over copy-paste. Do not think
for long before writing - plan briefly, then build. Reply with one short sentence when done.
""".strip()


def _skill_text() -> str:
    candidates = [
        str(config.app.get("storybook_skill_path", "") or "").strip(),
        os.getenv("STORYBOOK_SKILL_PATH", "").strip(),
        os.path.join(utils.root_dir(), DEFAULT_SKILL_PATH),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                text = f.read()
            # 去掉 YAML frontmatter，只保留正文方法论。
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    text = text[end + 4 :]
            return text.strip()
    raise FileNotFoundError(
        "hand-drawn-storybook-animation SKILL.md not found; download it from "
        "https://github.com/lhlGitHub/hand-drawn-storybook-animation-skill to "
        f"{DEFAULT_SKILL_PATH} or set app.storybook_skill_path"
    )


def _claude_command(resume_session: str = "") -> list[str]:
    configured_cli = str(
        config.app.get("claude_code_cli_path", "") or ""
    ).strip() or "claude"
    cli_path = shutil.which(configured_cli) or (
        configured_cli if os.path.isfile(configured_cli) else ""
    )
    if not cli_path:
        raise RuntimeError(
            f"claude CLI not found ('{configured_cli}'); the storybook source needs "
            "Claude Code installed and logged in (see docker-compose.claude.yml)"
        )
    command = [
        cli_path,
        "-p",
        "--output-format",
        "json",
        "--append-system-prompt",
        _skill_text() + "\n\n" + RENDER_CONTRACT,
        # 只给文件读写工具，权限模式自动接受工作目录内的编辑。
        "--tools",
        "Read,Write,Edit",
        "--permission-mode",
        "acceptEdits",
        "--safe-mode",
    ]
    model_name = str(
        config.app.get("storybook_claude_model", "")
        or config.app.get("claude_code_model_name", "")
        or ""
    ).strip()
    if model_name:
        command += ["--model", model_name]
    # 默认 medium：high 以上会先长时间思考，曾把 128k 输出额度全耗在思考上而
    # 一行代码都没写出来。
    effort = str(config.app.get("storybook_claude_effort", "") or "medium").strip()
    if effort:
        command += ["--effort", effort]
    if resume_session:
        command += ["--resume", resume_session]
    return command


def _run_claude(prompt: str, work_dir: str, resume_session: str = "") -> dict:
    raw_timeout = config.app.get("storybook_claude_timeout", "")
    timeout = (
        DEFAULT_CLAUDE_TIMEOUT
        if raw_timeout is None or not str(raw_timeout).strip()
        else llm.coerce_claude_code_timeout(raw_timeout, "storybook_claude_timeout")
    )
    cli_env, removed = llm.build_claude_code_env()
    if removed:
        logger.warning(
            "storybook: ignoring conflicting environment variables so the "
            f"subscription login is used: {', '.join(removed)}"
        )
    logger.info(f"storybook: invoking claude cli (timeout {timeout:.0f}s)")
    try:
        completed = subprocess.run(
            _claude_command(resume_session),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=work_dir,
            env=cli_env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude cli timed out after {timeout:.0f}s") from None
    stdout = (completed.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        payload = None
    if payload is None:
        detail = (completed.stderr or stdout or "").strip()
        raise RuntimeError(
            f"claude cli exited with code {completed.returncode}: {detail[:500]}"
        )
    if payload.get("is_error") or completed.returncode != 0:
        reason = str(payload.get("result") or "").strip() or (
            f"claude cli exited with code {completed.returncode}"
        )
        if "login" in reason.lower():
            reason += (
                " (run `claude setup-token` on the host and pass the token to the "
                "container as CLAUDE_CODE_OAUTH_TOKEN)"
            )
        raise RuntimeError(f"claude cli error: {reason[:500]}")
    logger.info(
        f"storybook: claude finished, turns={payload.get('num_turns')}, "
        f"result={str(payload.get('result') or '')[:200]}"
    )
    return payload


def _resolution(params: VideoParams) -> tuple[int, int]:
    return VideoAspect(params.video_aspect).to_resolution()


def build_animation(
    task_id: str, params: VideoParams, story: dict, timeline: Timeline
) -> str:
    """让 Claude 写出 index.html，加载校验失败时把错误发回同一会话修复。"""
    work_dir = utils.task_dir(os.path.join(task_id, "storybook"))
    with open(os.path.join(work_dir, "story.json"), "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)
    with open(os.path.join(work_dir, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump(timeline.to_dict(), f, ensure_ascii=False, indent=2)

    width, height = _resolution(params)
    style = STYLES.get(story.get("style") or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
    prompt = (
        "用 hand-drawn-storybook-animation 这个 skill，按这个剧本和分镜做一部手绘动画。\n"
        f"Use the hand-drawn-storybook-animation method to build the animation for "
        f"story.json / timeline.json in this directory.\n"
        f"- Style: {style}. One style and one colour grammar for the whole film.\n"
        f"- Output resolution: {width}x{height} "
        f"({'portrait 9:16' if height > width else 'landscape 16:9'}); compose for it.\n"
        f"- Total duration: {timeline.duration:.2f} s, {len(timeline.scenes)} scenes. "
        "Each scene needs designed shots (wide/medium/close-up/insert/low angle) - never "
        "a single uniform pan; chapters/scene changes use the skill's transitions "
        "(page curl, wet-brush wipe, ink bloom...).\n"
        "- Pencil/ink lines draw themselves in, watercolor blooms in, characters act "
        "(mouth moves while their own lines play; the narrator has no mouth).\n"
        "- Keep characters' designs fixed for the whole film (see 'look' in story.json)."
    )
    html_file = os.path.join(work_dir, "index.html")
    attempts = max(0, int(config.app.get("storybook_fix_attempts", 2) or 0))
    payload = _run_claude(prompt, work_dir)
    session_id = str(payload.get("session_id") or "")
    for attempt in range(attempts + 1):
        problems = (
            ["index.html was not written"]
            if not os.path.isfile(html_file)
            else validate_page(html_file, width, height, timeline.duration, work_dir)
        )
        if not problems:
            return html_file
        logger.warning(f"storybook: page check failed: {problems}")
        if attempt == attempts:
            break
        payload = _run_claude(
            "The recorder loaded index.html?render=1 and found these problems. Fix them "
            "in index.html (keep everything else the same):\n- " + "\n- ".join(problems),
            work_dir,
            resume_session=session_id,
        )
        session_id = str(payload.get("session_id") or session_id)
    raise RuntimeError(f"generated animation page is not usable: {'; '.join(problems)}")


# ---------------------------------------------------------------- 录制


_WAIT_READY_JS = """
async () => {
  for (let i = 0; i < 200 && !(window.STORY && typeof window.STORY.renderFrame === 'function'); i++) {
    await new Promise(r => setTimeout(r, 50));
  }
  if (!(window.STORY && typeof window.STORY.renderFrame === 'function')) return 'missing';
  if (window.STORY.ready && typeof window.STORY.ready.then === 'function') await window.STORY.ready;
  return 'ok';
}
"""

_BLANK_CHECK_JS = """
() => {
  const c = document.querySelector('canvas');
  if (!c || !c.width || !c.height) return -1;
  const probe = document.createElement('canvas');
  probe.width = 64; probe.height = 64;
  const g = probe.getContext('2d');
  g.drawImage(c, 0, 0, 64, 64);
  const d = g.getImageData(0, 0, 64, 64).data;
  let sum = 0, sq = 0, n = d.length / 4;
  for (let i = 0; i < d.length; i += 4) {
    const v = (d[i] + d[i + 1] + d[i + 2]) / 3; sum += v; sq += v * v;
  }
  const mean = sum / n;
  return Math.sqrt(Math.max(0, sq / n - mean * mean));
}
"""


def _launch(playwright, width: int, height: int):
    browser = playwright.chromium.launch(
        args=["--autoplay-policy=no-user-gesture-required", "--disable-gpu"]
    )
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    return browser, page


def validate_page(
    html_file: str, width: int, height: int, duration: float, work_dir: str = ""
) -> list[str]:
    """加载页面、抽几帧检查：报错、没有 STORY 接口、画面全空都算失败。"""
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    errors: list[str] = []
    with sync_playwright() as p:
        browser, page = _launch(p, width, height)
        try:
            page.on("pageerror", lambda exc: errors.append(f"page error: {exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console.error: {msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(f"file://{html_file}?render=1", wait_until="load")
            if page.evaluate(_WAIT_READY_JS) != "ok":
                problems.append("window.STORY.renderFrame is not defined after load")
            else:
                page_duration = page.evaluate("() => Number(window.STORY.duration)")
                if not page_duration or page_duration < duration * 0.9:
                    problems.append(
                        f"STORY.duration is {page_duration}, expected about {duration:.2f}"
                    )
                for index, fraction in enumerate((0.02, 0.2, 0.45, 0.7, 0.95)):
                    t = round(duration * fraction, 3)
                    try:
                        page.evaluate(f"() => window.STORY.renderFrame({t})")
                    except Exception as exc:
                        problems.append(f"renderFrame({t}) threw: {str(exc)[:300]}")
                        continue
                    spread = page.evaluate(_BLANK_CHECK_JS)
                    if spread is not None and 0 <= spread < 2:
                        problems.append(f"frame at t={t}s is blank (single flat colour)")
                    if spread == -1:
                        problems.append("no sized <canvas> element found")
                    if work_dir:
                        page.screenshot(
                            path=os.path.join(work_dir, f"preview-{index + 1}.jpg"),
                            type="jpeg",
                            quality=80,
                        )
        finally:
            browser.close()
    # 同一错误常在每帧重复出现，去重后再反馈给 Claude。
    for message in dict.fromkeys(errors):
        problems.append(message[:400])
    return problems[:12]


def record_video(
    html_file: str,
    output_file: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    report: Callable[[float], None] | None = None,
) -> None:
    """逐帧调用 renderFrame 截图，JPEG 管道送给 ffmpeg 编码成无声 H.264。"""
    from playwright.sync_api import sync_playwright

    total_frames = max(1, int(round(duration * fps)))
    ffmpeg = subprocess.Popen(
        [
            utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(fps), output_file,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with sync_playwright() as p:
            browser, page = _launch(p, width, height)
            try:
                page.goto(f"file://{html_file}?render=1", wait_until="load")
                if page.evaluate(_WAIT_READY_JS) != "ok":
                    raise RuntimeError("animation page did not expose STORY.renderFrame")
                for frame in range(total_frames):
                    page.evaluate("t => window.STORY.renderFrame(t)", frame / fps)
                    ffmpeg.stdin.write(page.screenshot(type="jpeg", quality=92))
                    if report and frame % fps == 0:
                        report(frame / total_frames)
            finally:
                browser.close()
    finally:
        if ffmpeg.stdin:
            ffmpeg.stdin.close()
        stderr = ffmpeg.stderr.read().decode("utf-8", errors="replace") if ffmpeg.stderr else ""
        if ffmpeg.wait() != 0:
            raise RuntimeError(f"ffmpeg failed to encode frames: {stderr[:500]}")


# ---------------------------------------------------------------- 编排


def _progress(task_id: str, start: float, end: float) -> Callable[[float], None]:
    def report(fraction: float) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_PROCESSING,
                progress=int(start + (end - start) * fraction),
            )
        except Exception as exc:
            logger.debug(f"storybook: progress update failed: {exc}")

    return report


def run(task_id: str, params: VideoParams, stop_at: str = "video", fail=None) -> dict:
    """执行故事动画流水线；``fail(stage, message)`` 由 task 模块传入以统一失败结构。"""

    def _fail(stage: str, message: str) -> dict:
        if fail is not None:
            return fail(task_id, stage, message)
        raise RuntimeError(f"{stage}: {message}")

    if not utils.check_ffmpeg_ready():
        return _fail("preflight", "ffmpeg is not available")
    try:
        import playwright  # noqa: F401
    except ImportError:
        if stop_at == "video":
            return _fail(
                "preflight",
                "playwright is not installed; run `pip install playwright && "
                "playwright install --with-deps chromium` (or use Dockerfile.claude)",
            )
    try:
        _skill_text()
    except FileNotFoundError as exc:
        return _fail("preflight", str(exc))

    app_config = config.snapshot_config_with_pending(config.app)
    task_dir = utils.task_dir(task_id)

    try:
        story = generate_story(params, app_config=app_config)
    except Exception as exc:
        logger.exception("storybook: story generation failed")
        return _fail("script", f"{type(exc).__name__}: {exc}")
    narration = "\n".join(
        line["text"] for scene in story["scenes"] for line in scene["lines"]
    )
    task_artifacts.write_script_data(
        task_id,
        {"script": narration, "search_terms": [], "storyboard": story, "params": params},
    )
    sm.state.update_task(
        task_id, state=const.TASK_STATE_PROCESSING, progress=15, script=narration
    )
    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=narration
        )
        return {"script": narration, "storyboard": story}

    try:
        timeline = synthesize_voice(task_id, params, story, _progress(task_id, 15, 30))
        audio_file = os.path.join(task_dir, "audio.mp3")
        write_audio(timeline, audio_file)
        subtitle_path = os.path.join(task_dir, "subtitle.srt")
        write_subtitles(timeline, subtitle_path)
    except Exception as exc:
        logger.exception("storybook: voice synthesis failed")
        return _fail("audio", f"{type(exc).__name__}: {exc}")
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=30,
        audio_file=audio_file,
        audio_duration=timeline.duration,
        subtitle_path=subtitle_path,
    )
    if stop_at in {"audio", "subtitle"}:
        sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100)
        return {"audio_file": audio_file, "subtitle_path": subtitle_path}

    try:
        html_file = build_animation(task_id, params, story, timeline)
    except Exception as exc:
        logger.exception("storybook: animation generation failed")
        return _fail("materials", f"{type(exc).__name__}: {exc}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=60)

    width, height = _resolution(params)
    fps = int(config.app.get("storybook_fps", DEFAULT_FPS) or DEFAULT_FPS)
    combined_file = os.path.join(task_dir, "combined-1.mp4")
    final_file = os.path.join(task_dir, "final-1.mp4")
    try:
        record_video(
            html_file, combined_file, width, height, timeline.duration, fps,
            _progress(task_id, 60, 90),
        )
        bgm_ok = video.generate_video(
            video_path=combined_file,
            audio_path=audio_file,
            subtitle_path=subtitle_path if params.subtitle_enabled else "",
            output_file=final_file,
            params=params,
        )
    except Exception as exc:
        logger.exception("storybook: rendering failed")
        return _fail("video", f"{type(exc).__name__}: {exc}")

    result = {
        "videos": [final_file],
        "combined_videos": [combined_file],
        "script": narration,
        "terms": [],
        "audio_file": audio_file,
        "audio_duration": timeline.duration,
        "subtitle_path": subtitle_path,
        "materials": [],
        "warnings": None if bgm_ok else [{"code": "storybook_bgm_failed", "video_index": 1}],
    }
    sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **result)
    logger.success(f"storybook task {task_id} finished: {final_file}")
    return result
