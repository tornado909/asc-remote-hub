"""Prevent publishing local workstation labels or standalone release diaries."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST_LABEL = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]{2,}-(?:PC|SERVER)(?![A-Za-z0-9_-])")


class PublicIdentifierPolicy(unittest.TestCase):
    def test_no_standalone_changelog(self):
        self.assertFalse((ROOT / "CHANGELOG.md").exists())

    def test_no_local_device_labels_in_tracked_files(self):
        paths = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT
        ).split(b"\0")
        problems = []
        for raw in paths:
            if not raw:
                continue
            path = ROOT / raw.decode("utf-8")
            if not path.is_file():
                continue
            try:
                data = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            for label in HOST_LABEL.findall(data):
                if not label.lower().startswith(("example-", "sample-", "test-", "dummy-")):
                    problems.append(str(path.relative_to(ROOT)))
        self.assertEqual(problems, [], "Workstation identifiers must not be committed")


if __name__ == "__main__":
    unittest.main()
