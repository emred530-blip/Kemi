"""T8 media.transcode (capability-gated) and T10 fleet map."""

import shutil
import unittest


class FfmpegTaskTests(unittest.TestCase):
    def test_arg_builder_is_safe(self):
        from kemi.tasks import _ffmpeg_args

        args = _ffmpeg_args("/t/in", "/t/out.mp3", "mp3", ["-b:a", "128k"])
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("-nostdin", args)        # no stdin prompts
        self.assertEqual(args[-1], "/t/out.mp3")
        self.assertNotIn(";", " ".join(args))  # no shell metacharacters

    def test_format_allowlist(self):
        from kemi.tasks import TaskError, _ffmpeg_args

        for ok in ("mp3", "wav", "mp4", "gif", "png"):
            _ffmpeg_args("a", f"b.{ok}", ok)
        for bad in ("exe", "sh", "../x", ""):
            with self.assertRaises(TaskError):
                _ffmpeg_args("a", "b", bad)

    def test_capability_gated_registration(self):
        from kemi.tasks import TASKS

        # registered iff ffmpeg is on PATH (like sci.matmul with numpy)
        self.assertEqual("media.transcode" in TASKS, shutil.which("ffmpeg") is not None)


class FleetMapTests(unittest.TestCase):
    def test_dashboard_has_fleet_map(self):
        from kemi.webui import _PAGE

        self.assertIn('id="fleetmap"', _PAGE)
        self.assertIn("Network map", _PAGE)
        self.assertIn("Ağ haritası", _PAGE)        # bilingual
        # the map is drawn from live provider data + this node
        self.assertIn("s.providers.slice", _PAGE)
        self.assertIn("(you)", _PAGE)


if __name__ == "__main__":
    unittest.main()
