"""UI regression checks for the task-focused Remote Hub account workspace."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
CSS=(ROOT/"server/static/style.css").read_text(encoding="utf8")
HTML=(ROOT/"server/static/dashboard.html").read_text(encoding="utf8")
JS=(ROOT/"server/static/dashboard.js").read_text(encoding="utf8")


class PanelSpacingTests(unittest.TestCase):
    def test_consistent_workspace_tokens_and_spacings(self):
        self.assertIn("body.hub-app {",CSS)
        for token in ("--bg:","--surface:","--border:","--txt:","--muted:","--accent:"):
            self.assertIn(token,CSS)
        self.assertIn(".hub-app .hub-view > .view-heading + .card",CSS)
        self.assertIn(".hub-app .hub-content",CSS)
        self.assertNotIn(".hub-app .hub-view>.card:first-of-type",CSS)

    def test_advanced_onboarding_as_accessible_disclosures(self):
        for name,desc in (
            ("Дополнительная установка","Если нет Node.js или Python"),
            ("Обслуживание агента","Отвязка и удаление"),
        ):
            self.assertIn('<span class="advanced-label"><strong>'+name+
                          '</strong><small>'+desc+'</small></span>',HTML)
        self.assertIn('<details class="advanced-block" id="install-advanced">',HTML)
        self.assertIn('<details class="advanced-block" id="manage-agent">',HTML)
        self.assertIn(".hub-app .advanced-label",CSS)

    def test_usage_is_semantic_table_not_ornamental_bars(self):
        self.assertIn('<table class="usage-table">',HTML)
        self.assertIn('<tbody id="usage" aria-live="polite">',HTML)
        self.assertEqual(HTML.count('scope="col"'),4)
        self.assertIn('role="group" aria-label="Сводка использования"',HTML)
        self.assertNotIn('usage-bar',JS)
        self.assertIn("document.createElement('tr')",JS)
        self.assertIn("err.className='usage-errors'",JS)
        self.assertIn(".hub-app .usage-table",CSS)
        self.assertIn('grid-template-areas: "tool calls" "date errors"',CSS)

    def test_error_indicator_independent_and_focus_visible(self):
        self.assertIn('<span class="errors-summary-title">Журнал ошибок</span>'
                      '<span id="errors-header-count" class="errors-summary-count">—</span>',HTML)
        self.assertIn(".hub-app .errors-main-summary::after",CSS)
        self.assertIn("):focus-visible",CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)",CSS)

    def test_primary_action_meets_text_contrast(self):
        def luminance(hex_color):
            values=[int(hex_color[i:i+2],16)/255 for i in (1,3,5)]
            linear=[x/12.92 if x<=0.04045 else ((x+0.055)/1.055)**2.4 for x in values]
            return sum(a*b for a,b in zip(linear,(0.2126,0.7152,0.0722)))
        for selector in (
            ".hub-app button.primary",
            ".hub-app button.primary:hover:not(:disabled)",
        ):
            found=re.search(re.escape(selector)+r"\s*\{([^}]*)\}",CSS)
            self.assertIsNotNone(found,selector)
            match=re.search(r"background:\s*(#[0-9a-fA-F]{6})",found.group(1))
            self.assertIsNotNone(match,selector)
            background=luminance(match.group(1))
            ratio=(1.05)/(background+0.05)
            self.assertGreaterEqual(ratio,4.5,(selector,ratio))

    def test_no_decorative_client_icons_or_hardcoded_account_avatar(self):
        self.assertNotIn('class="client-icon',HTML)
        self.assertNotIn('class="account-avatar',HTML)
        self.assertNotIn('class="nav-glyph',HTML)
        self.assertIn('id="account-email"',HTML)


if __name__=="__main__":
    unittest.main()
