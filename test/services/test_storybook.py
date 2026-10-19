import base64
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoParams
from app.services import doubao_tts, storybook
from app.services import task as tm


STORYBOARD = {
    "title": "小萤火虫",
    "characters": [
        {"id": "firefly", "name": "小萤", "look": "round", "voice": "shy little girl"},
        {"id": "blot", "name": "墨点", "look": "ink", "voice": "tiny creature, cute sound"},
    ],
    "scenes": [
        {"shot": "wide", "visual": "night", "lines": [{"speaker": "narrator", "text": "夜深了。"}]},
        {"shot": "close-up", "visual": "glow", "lines": ["我不怕黑。", {"speaker": "firefly", "text": " "}]},
        {"shot": "insert", "visual": "pause", "lines": []},
    ],
}


class TestStoryboardParsing(unittest.TestCase):
    def test_parses_fenced_json_and_normalizes_lines(self):
        story = storybook._parse_story_json(
            "```json\n" + json.dumps(STORYBOARD, ensure_ascii=False) + "\n```"
        )
        self.assertEqual([scene["id"] for scene in story["scenes"]], [1, 2, 3])
        # 纯字符串台词归为旁白，空白台词被丢弃。
        self.assertEqual(
            story["scenes"][1]["lines"], [{"speaker": "narrator", "text": "我不怕黑。"}]
        )

    def test_rejects_storyboard_without_spoken_lines(self):
        with self.assertRaises(ValueError):
            storybook._parse_story_json(json.dumps({"scenes": [{"lines": []}]}))


class TestExplainerGenre(unittest.TestCase):
    def test_explainer_uses_teaching_prompts_and_verbatim_storyboard(self):
        prompts = []
        responses = iter(["draft", "revised script", json.dumps(STORYBOARD, ensure_ascii=False)])

        def fake_llm(prompt, app_config=None):
            prompts.append(prompt)
            return next(responses)

        params = VideoParams(video_subject="Transformer", storybook_genre="explainer")
        with patch.object(storybook, "_ask_llm", side_effect=fake_llm):
            story = storybook.generate_story(params)

        self.assertEqual(story["genre"], "explainer")
        self.assertIn("EXPLAINER", prompts[0])
        self.assertIn("NEVER heard", prompts[1])
        self.assertIn("VERBATIM", prompts[2])
        self.assertIn("revised script", prompts[2])
        self.assertNotIn("治愈", "".join(prompts))

    def test_story_genre_keeps_original_prompts(self):
        prompts = []
        responses = iter(["draft", "revised", json.dumps(STORYBOARD, ensure_ascii=False)])
        params = VideoParams(video_subject="x")
        with patch.object(
            storybook, "_ask_llm", side_effect=lambda p, a=None: prompts.append(p) or next(responses)
        ):
            storybook.generate_story(params)
        self.assertIn("治愈", prompts[0])
        self.assertNotIn("VERBATIM", prompts[2])


class TestTimeline(unittest.TestCase):
    def test_lines_follow_real_audio_and_short_scenes_get_minimum_length(self):
        story = storybook._parse_story_json(json.dumps(STORYBOARD, ensure_ascii=False))
        params = VideoParams(video_subject="x", storybook_use_doubao=False)
        durations = iter([2.0, 1.0])
        with tempfile.TemporaryDirectory() as root, patch.object(
            storybook.utils, "task_dir", side_effect=lambda sub="": os.path.join(root, sub)
        ), patch.object(
            storybook, "_speak_line_mpt", side_effect=lambda *_: next(durations)
        ):
            timeline = storybook.synthesize_voice("t", params, story)

        first, second = timeline.lines
        self.assertAlmostEqual(first.start, storybook.LEAD_IN)
        self.assertAlmostEqual(first.end, storybook.LEAD_IN + 2.0)
        scene1, scene2, scene3 = timeline.scenes
        # 场景 1 只有 2 秒台词，被补足到最短场景长度，下一句随之顺延。
        self.assertAlmostEqual(scene1["end"] - scene1["start"], storybook.MIN_SCENE)
        self.assertAlmostEqual(second.start, scene1["end"])
        self.assertEqual(scene3["end"], round(timeline.duration, 3))

    def test_srt_uses_line_times_and_splits_long_lines(self):
        timeline = storybook.Timeline(
            duration=10,
            lines=[
                storybook.TimedLine(1, "narrator", "短句。", 1.0, 2.0),
                storybook.TimedLine(
                    1, "narrator", "这是一句很长很长的旁白，需要拆开。后面还有第二句话，也很长很长。", 3.0, 7.0
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as root:
            srt = os.path.join(root, "a.srt")
            storybook.write_subtitles(timeline, srt)
            content = Path(srt).read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:02,000\n短句。", content)
        self.assertIn("\n3\n", content)
        self.assertTrue(content.rstrip().endswith("也很长很长。"))
        self.assertIn("--> 00:00:07,000", content)


class TestDoubaoTts(unittest.TestCase):
    def _response(self, body: bytes):
        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response(body)

    def test_concatenates_pcm_chunks_and_caches(self):
        pcm = b"\x01\x00" * doubao_tts.SAMPLE_RATE  # 1 秒
        half = len(pcm) // 2
        body = "\n".join(
            json.dumps({"code": 0, "data": base64.b64encode(chunk).decode()})
            for chunk in (pcm[:half], pcm[half:])
        ) + "\n" + json.dumps({"code": 20000000}) + "\n"
        settings = {"doubao_tts_api_key": "k"}
        with tempfile.TemporaryDirectory() as root, patch.object(
            doubao_tts.urllib.request, "urlopen", return_value=self._response(body.encode())
        ) as urlopen:
            out = os.path.join(root, "a.wav")
            seconds = doubao_tts.synthesize(
                "你好", out, context="温柔", cache_dir=os.path.join(root, "c"), settings=settings
            )
            again = doubao_tts.synthesize(
                "你好", out, context="温柔", cache_dir=os.path.join(root, "c"), settings=settings
            )
            with wave.open(out) as wav:
                frames = wav.getnframes()

        self.assertAlmostEqual(seconds, 1.0)
        self.assertAlmostEqual(again, 1.0)
        self.assertEqual(frames, doubao_tts.SAMPLE_RATE)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "k")
        additions = json.loads(json.loads(request.data)["req_params"]["additions"])
        self.assertEqual(additions["context_texts"], ["温柔"])

    def test_error_code_raises(self):
        body = json.dumps({"code": 45000000, "message": "quota"}).encode()
        with tempfile.TemporaryDirectory() as root, patch.object(
            doubao_tts.urllib.request, "urlopen", return_value=self._response(body)
        ):
            with self.assertRaisesRegex(RuntimeError, "45000000"):
                doubao_tts.synthesize(
                    "你好", os.path.join(root, "a.wav"), settings={"doubao_tts_api_key": "k"}
                )


class TestTaskDispatch(unittest.TestCase):
    def test_storybook_source_runs_storybook_pipeline(self):
        params = VideoParams(video_subject="x", video_source="storybook")
        with patch.object(tm.sm.state, "update_task"), patch.object(
            tm.storybook, "run", return_value={"videos": ["v"]}
        ) as run:
            result = tm.start("task-sb", params)
        self.assertEqual(result, {"videos": ["v"]})
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["fail"], tm._mark_task_failed)


if __name__ == "__main__":
    unittest.main()
