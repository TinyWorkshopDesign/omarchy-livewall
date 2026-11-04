"""Tests for livewall.py, the translation catalogs and the QML text sinks.

Run from the plugin folder:  python3 -m unittest discover -s tests
No network access: providers are fed recorded-style data.
"""

import importlib.util
import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("livewall", ROOT / "livewall.py")
lw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lw)


PIXABAY_HIT = {
    "id": 125,
    "pageURL": "https://pixabay.com/videos/ocean-sea-waves-125/",
    "tags": "ocean, sea, waves, beach",
    "duration": 12,
    "user": "someone",
    "videos": {
        "large": {"url": "https://cdn.pixabay.com/v/125_large.mp4", "width": 3840, "height": 2160,
                  "thumbnail": "https://cdn.pixabay.com/v/125_large.jpg"},
        "medium": {"url": "https://cdn.pixabay.com/v/125_medium.mp4", "width": 1920, "height": 1080,
                   "thumbnail": "https://cdn.pixabay.com/v/125_medium.jpg"},
        "small": {"url": "https://cdn.pixabay.com/v/125_small.mp4", "width": 1280, "height": 720,
                  "thumbnail": "https://cdn.pixabay.com/v/125_small.jpg"},
        "tiny": {"url": "http://cdn.pixabay.com/v/125_tiny.mp4", "width": 960, "height": 540,
                 "thumbnail": "https://cdn.pixabay.com/v/125_tiny.jpg"},
    },
}


class Selection(unittest.TestCase):
    files = [[3840, 2160, "a"], [2560, 1440, "b"], [1920, 1080, "c"], [1280, 720, "d"]]

    def test_pick_each_quality(self):
        for q, want in ((720, "d"), (1080, "c"), (1440, "b"), (2160, "a")):
            self.assertEqual(lw.pick(self.files, q)[2], want, q)

    def test_pick_falls_back_to_largest_available(self):
        self.assertEqual(lw.pick([[1920, 1080, "c"]], 2160)[2], "c")

    def test_pick_smallest_when_all_too_big(self):
        self.assertEqual(lw.pick([[3840, 2160, "a"], [2560, 1440, "b"]], 720)[2], "b")

    def test_pick_ignores_broken_entries(self):
        self.assertIsNone(lw.pick([[0, 0, "x"]], 1080))
        self.assertIsNone(lw.pick([], 1080))

    def test_vclass_non_16_9(self):
        self.assertEqual(lw.vclass(4096, 2160), 2160)
        self.assertEqual(lw.vclass(1920, 1012), 1012)
        self.assertEqual(lw.vclass(1080, 1920), 608)   # portrait


class Monitors(unittest.TestCase):
    def test_needed(self):
        self.assertEqual(lw.needed(1920, 1080), 1080)
        self.assertEqual(lw.needed(1920, 1200), 1080)    # 11% enlargement is fine
        self.assertEqual(lw.needed(2560, 1440), 1440)
        self.assertEqual(lw.needed(3440, 1440), 2160)    # ultrawide needs 4K width
        self.assertEqual(lw.needed(1366, 768), 720)
        self.assertEqual(lw.needed(7680, 4320), 2160)    # capped at the largest class

    def run_monitors(self, data):
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr="")
        with mock.patch.object(lw.subprocess, "run", return_value=fake):
            return lw.monitors()

    def test_monitors_rotated_disabled_and_names(self):
        mons = self.run_monitors([
            {"name": "DP-1", "width": 2560, "height": 1440, "transform": 1},
            {"name": "HDMI-A-1", "width": 1920, "height": 1080, "disabled": True},
            {"name": "eDP-1<b>", "width": 1920, "height": 1200, "transform": 0},
        ])
        self.assertEqual([m["name"] for m in mons], ["DP-1", "eDP-1b"])
        self.assertEqual((mons[0]["width"], mons[0]["height"]), (1440, 2560))
        self.assertEqual(lw.auto_quality(mons), 2160)

    def test_monitors_survive_bad_output(self):
        with mock.patch.object(lw.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(lw.monitors(), [])
        self.assertEqual(self.run_monitors({"not": "a list"}), [])
        self.assertEqual(lw.auto_quality([]), 1440)

    def test_resolve_quality(self):
        self.assertEqual(lw.resolve_quality("720", []), 720)
        self.assertEqual(lw.resolve_quality("auto", []), 1440)
        self.assertEqual(lw.resolve_quality("999", [{"need": 1080}]), 1080)


class Providers(unittest.TestCase):
    def test_pixabay_normalise(self):
        n = lw.PROVIDERS["pixabay"]._norm(PIXABAY_HIT)
        self.assertEqual(n["id"], 125)
        self.assertEqual(n["slug"], "ocean-sea-waves")
        self.assertEqual(n["title"], "ocean, sea, waves")
        self.assertTrue(n["thumbUrl"].endswith("125_tiny.jpg"))
        # The plain-http rendition is dropped.
        self.assertEqual(len(n["files"]), 3)

    def test_pixabay_rejects_foreign_hosts(self):
        hit = json.loads(json.dumps(PIXABAY_HIT))
        hit["videos"]["medium"]["url"] = "https://evil.example/x.mp4"
        hit["videos"]["tiny"]["thumbnail"] = "https://evil.example/x.jpg"
        n = lw.PROVIDERS["pixabay"]._norm(hit)
        self.assertNotIn("https://evil.example/x.mp4", [f[2] for f in n["files"]])
        self.assertFalse(n["thumbUrl"].startswith("https://evil.example"))

    def test_pexels_normalise(self):
        v = {"id": 7, "url": "https://www.pexels.com/video/rain-on-glass-7/", "duration": 9,
             "image": "https://images.pexels.com/videos/7/p.jpeg?x=1", "user": {"name": "A"},
             "video_files": [
                 {"file_type": "video/mp4", "width": 2560, "height": 1440, "link": "https://videos.pexels.com/b.mp4"},
                 {"file_type": "video/webm", "width": 1920, "height": 1080, "link": "https://videos.pexels.com/c.webm"},
             ]}
        n = lw.PROVIDERS["pexels"]._norm(v)
        self.assertEqual(n["slug"], "rain-on-glass")
        self.assertEqual(len(n["files"]), 1)
        self.assertIn("w=480", n["thumbUrl"])

    def test_slug(self):
        self.assertEqual(lw.slug_from_url("https://pixabay.com/videos/id-125/", "videos"), "")
        self.assertEqual(lw.slug_from_url("https://x/videos/a-b-1/", "videos"), "a-b")

    def test_keys(self):
        self.assertTrue(lw.Pixabay.valid_key("12345678-0123456789abcdef01234567"))
        self.assertFalse(lw.Pixabay.valid_key("abc; rm -rf ~"))
        self.assertFalse(lw.Pexels.valid_key("with-dash-0123456789abcdef"))


class Untrusted(unittest.TestCase):
    """Markup-shaped data from a provider must reach the panel unchanged, as data."""

    def test_markup_title_is_kept_verbatim(self):
        hit = json.loads(json.dumps(PIXABAY_HIT))
        hit["tags"] = '<img src="https://example.invalid/t">, <b>x</b>'
        hit["user"] = "<a href='https://example.invalid'>me</a>"
        n = lw.PROVIDERS["pixabay"]._norm(hit)
        self.assertIn("<img", n["title"])
        self.assertLessEqual(len(n["title"]), 60)
        self.assertLessEqual(len(n["author"]), 60)

    def test_every_qml_text_is_plain(self):
        """Every Text in the plugin goes through the PlainText component."""
        for qml in ROOT.glob("*.qml"):
            src = qml.read_text()
            # The PlainText component's own definition is the one allowed Text.
            without_component = re.sub(r"component\s+PlainText\s*:\s*Text\s*\{", "", src)
            bare = [m.start() for m in re.finditer(r"(?<![\w.])Text\s*\{", without_component)]
            self.assertEqual(bare, [], f"{qml.name}: bare Text {{ }} found")
            for m in re.finditer(r"component\s+PlainText\s*:\s*Text\s*\{([^}]*)\}", src):
                self.assertIn("Text.PlainText", m.group(1))
        self.assertNotIn("RichText", "".join(p.read_text() for p in ROOT.glob("*.qml")))
        self.assertNotIn("StyledText", "".join(p.read_text() for p in ROOT.glob("*.qml")))


class Catalogs(unittest.TestCase):
    def test_catalogs_match_english(self):
        en = json.loads((ROOT / "i18n/en.json").read_text())

        def holes(v):
            vals = v.values() if isinstance(v, dict) else [v]
            return {h for s in vals for h in re.findall(r"%\d", s)}

        for path in sorted((ROOT / "i18n").glob("*.json")):
            cat = json.loads(path.read_text())
            for key, value in cat.items():
                self.assertIn(key, en, f"{path.name}: unknown key {key}")
                self.assertEqual(holes(value), holes(en[key]), f"{path.name}: {key} placeholders")
                if isinstance(value, dict):
                    self.assertIn("other", value, f"{path.name}: {key} lacks 'other'")

    def test_full_catalogs_are_complete(self):
        en = set(json.loads((ROOT / "i18n/en.json").read_text()))
        for code in ("it", "de", "fr", "es", "pt", "nl", "pl"):
            cat = set(json.loads((ROOT / f"i18n/{code}.json").read_text()))
            self.assertEqual(en - cat, set(), code)

    def test_error_codes_have_strings(self):
        en = json.loads((ROOT / "i18n/en.json").read_text())
        src = (ROOT / "livewall.py").read_text()
        codes = set(re.findall(r'(?:fail|LWError)\("([a-z_]+)"', src))
        for code in codes:
            self.assertIn(f"error.{code}", en, code)


if __name__ == "__main__":
    unittest.main()
