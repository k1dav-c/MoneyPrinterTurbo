import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import compress
from app.utils import utils


class TestCompressPlan(unittest.TestCase):
    def test_keeps_1080p_when_bitrate_is_enough(self):
        video_kbps, short_side = compress.plan(100, 20 * 1024 * 1024, 1080)
        self.assertEqual(short_side, 1080)
        self.assertGreater(video_kbps, 1200)

    def test_downscales_long_videos(self):
        # 10 分钟压到 20MB，码率不足以支撑 1080p。
        _, short_side = compress.plan(600, 20 * 1024 * 1024, 1080)
        self.assertLess(short_side, 1080)

    def test_never_upscales(self):
        _, short_side = compress.plan(10, 20 * 1024 * 1024, 720)
        self.assertEqual(short_side, 720)


class TestCompressToLimit(unittest.TestCase):
    def _make_video(self, path):
        subprocess.run(
            [
                utils.get_ffmpeg_binary(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-b:v", "4M", "-c:a", "aac", "-shortest", path,
            ],
            check=True,
        )

    def test_small_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.mp4")
            self._make_video(path)
            before = os.path.getmtime(path)
            self.assertIsNone(compress.compress_to_limit(path, 50))
            self.assertEqual(os.path.getmtime(path), before)

    def test_large_file_is_compressed_under_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.mp4")
            self._make_video(path)
            limit_mb = os.path.getsize(path) / 1048576 / 3
            info = compress.compress_to_limit(path, limit_mb)
            self.assertIsNotNone(info)
            self.assertLessEqual(os.path.getsize(path), limit_mb * 1048576)
            self.assertEqual(os.listdir(root), ["a.mp4"])


if __name__ == "__main__":
    unittest.main()
