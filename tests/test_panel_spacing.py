"""Layout regression checks for focused Remote Hub screens."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "server/static/style.css").read_text(encoding="utf8")
HTML = (ROOT / "server/static/dashboard.html").read_text(encoding="utf8")
JS = (ROOT / "server/static/dashboard.js").read_text(encoding="utf8")


class PanelSpacingTests(unittest.TestCase):
    def test_heterogeneous_sections_keep_spacing(self):
        self.assertNotIn(".hub-app .hub-view>.card:first-of-type{margin-top:0;}", CSS)
        self.assertIn(".hub-app .hub-view>.view-heading+.card{margin-top:0;}", CSS)
        self.assertIn(".hub-app .hub-view>.card{margin-top:18px;}", CSS)
        self.assertIn(".hub-app #view-usage>.stats+.card{margin-top:20px;}", CSS)
        self.assertIn(".hub-app #view-usage>.errors-collapsed{margin-top:18px;}", CSS)

    def test_accordion_headings_have_grouped_subtitles(self):
        for title, hint in (
            ("Дополнительная установка", "Если нет Node.js или Python"),
            ("Обслуживание агента", "Отвязка и удаление"),
        ):
            self.assertIn(
                '<span class="advanced-label"><strong>' + title
                + '</strong><small>' + hint + '</small></span>', HTML)
        self.assertIn(".hub-app .advanced-label{display:flex;flex-direction:column;", CSS)
        self.assertIn(".hub-app .advanced-block>summary::after{margin-left:auto;", CSS)

    def test_error_count_is_badge_next_to_title_not_nested(self):
        self.assertIn(
            '<span class="errors-summary-title">Журнал ошибок</span>'
            '<span id="errors-header-count" class="errors-summary-count">—</span>',
            HTML)
        self.assertIn(".hub-app .errors-main-summary::after{margin-left:auto;", CSS)

    def test_usage_has_fixed_columns_and_distinct_error_label(self):
        self.assertIn(
            "grid-template-columns:minmax(0,1fr) 78px 140px;", CSS)
        self.assertIn(".hub-app .usage-row .usage-errors{", CSS)
        self.assertIn("err.textContent=row.errors===1?'1 ошибка':row.errors+' ош.';", JS)
        self.assertIn(".hub-app .usage-row .usage-bar{display:none;}", CSS)


if __name__ == "__main__":
    unittest.main()
