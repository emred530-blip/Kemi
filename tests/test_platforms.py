"""Cross-platform packaging artefacts and PWA assets (iOS/Android/desktop)."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PwaAssetTests(unittest.TestCase):
    def test_manifest_is_valid_and_standalone(self):
        from kemi.webui import _MANIFEST

        m = json.loads(_MANIFEST)
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(m["icons"])
        self.assertTrue(any(i["src"].endswith(".svg") for i in m["icons"]))
        self.assertTrue(any(i["src"].endswith(".png") for i in m["icons"]))

    def test_icon_png_is_valid(self):
        from kemi.webui import _icon_png

        png = _icon_png()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn(b"IHDR", png[:32])
        self.assertGreater(len(png), 100)

    def test_service_worker_skips_api(self):
        from kemi.webui import _SERVICE_WORKER

        # data endpoints must stay live, never served stale from cache
        self.assertIn("/api/", _SERVICE_WORKER)
        self.assertIn("return;", _SERVICE_WORKER)

    def test_page_has_mobile_and_pwa_tags(self):
        from kemi.webui import _PAGE

        for needle in ("viewport", "manifest.webmanifest",
                       "apple-mobile-web-app-capable", "theme-color",
                       "serviceWorker", "max-width:640px"):
            self.assertIn(needle, _PAGE, needle)


class PackagingArtefactTests(unittest.TestCase):
    def test_windows_powershell_installer(self):
        text = (ROOT / "scripts" / "install.ps1").read_text()
        self.assertIn("venv", text)
        self.assertIn("pip install", text)
        self.assertIn("kemi app", text)

    def test_pyinstaller_recipe_present(self):
        self.assertTrue((ROOT / "packaging" / "kemi.spec").exists())
        entry = (ROOT / "packaging" / "entry.py").read_text()
        self.assertIn('["app"]', entry)  # defaults to friendly app mode
        spec = (ROOT / "packaging" / "kemi.spec").read_text()
        self.assertIn("BUNDLE", spec)    # macOS .app bundle
        self.assertIn("hiddenimports", spec)

    def test_platforms_doc_covers_all_five(self):
        text = (ROOT / "PLATFORMS.md").read_text()
        for os_name in ("iOS", "Android", "macOS", "Windows", "Linux"):
            self.assertIn(os_name, text)


if __name__ == "__main__":
    unittest.main()
