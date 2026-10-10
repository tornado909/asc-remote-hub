"""Regression checks for GitHub README Markdown rendering.

This test uses only the standard library so CI doesn't need another package.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReadmeMarkdownTests(unittest.TestCase):
    def check_document(self, name: str, minimum_fences: int) -> None:
        raw = (ROOT / name).read_text(encoding="utf-8")
        self.assertNotIn(chr(92) + chr(96), raw, "Backticks must not be escaped")
        fences = [line for line in raw.splitlines() if line.startswith(chr(96) * 3)]
        self.assertGreaterEqual(len(fences), minimum_fences)
        self.assertEqual(len(fences) % 2, 0, "Unbalanced Markdown code fences")
        for index, line in enumerate(fences):
            if index % 2 == 0:
                self.assertRegex(line, r"^" + chr(96) * 3 + r"[a-zA-Z0-9_-]*$")
            else:
                self.assertEqual(line, chr(96) * 3)
        self.assertIn(chr(96) + "ASC_HUB_URL" + chr(96), raw)
        self.assertIn(chr(96) * 3 + "bash\n", raw)
        self.assertIn("https://hub.example.com/cli-1.0.0.tgz", raw)
        self.assertIn("https://hub.example.com/mcp", raw)

    def test_english_readme(self):
        self.check_document("README.md", 12)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(chr(96) * 3 + "dotenv\n", readme)
        self.assertIn("| Windows (PowerShell) | " + chr(96), readme)

    def test_russian_readme(self):
        self.check_document("README.ru.md", 10)
        self.assertIn(chr(96) * 3 + "powershell\n",
                      (ROOT / "README.ru.md").read_text(encoding="utf-8"))
