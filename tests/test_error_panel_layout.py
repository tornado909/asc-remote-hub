"""Prevent compressed error rows when the collapsible list overflows."""
import re
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class ErrorPanelLayoutTests(unittest.TestCase):
    def setUp(self):
        self.css=(ROOT/"server/static/style.css").read_text(encoding="utf-8")
        self.html=(ROOT/"server/static/dashboard.html").read_text(encoding="utf-8")

    def test_overflow_does_not_shrink_error_cards(self):
        # The initial flex column shrank all 15 cards into thin strips
        # once max-height/scrolling was introduced.
        self.assertIn(".errors-list{display:block;max-height:480px;",self.css)
        self.assertIn(".errors-list>.error-row{display:block;",self.css)
        self.assertIn("margin:0 0 10px;flex:none",self.css)
        self.assertIn(".errors-list>.error-row>.error-summary{display:block;min-height:76px;",self.css)
        self.assertNotIn(r"\n.errors-caption-open",self.css)

    def test_default_collapsed_and_caption_tracks_state(self):
        self.assertIn('<details class="card errors-collapsed" id="errors-section">',self.html)
        self.assertNotIn('<details class="card errors-collapsed" id="errors-section" open',self.html)
        self.assertIn("errors-caption-collapsed",self.html)
        self.assertIn("errors-caption-open",self.html)
        self.assertIn(".errors-collapsed[open] .errors-caption-open{display:inline}",self.css)
        self.assertIn(".errors-collapsed[open] .errors-caption-collapsed{display:none}",self.css)
