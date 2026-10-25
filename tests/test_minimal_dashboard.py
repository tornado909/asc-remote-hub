"""Minimal account UX must preserve existing authenticated workflows.

Only Python stdlib; public login routes and OAuth server are exercised by
their independent integration suites.
"""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
HTML=(ROOT/"server/static/dashboard.html").read_text("utf8")
CSS=(ROOT/"server/static/style.css").read_text("utf8")
JS=(ROOT/"server/static/dashboard.js").read_text("utf8")


class DOM(HTMLParser):
    VOID={"meta","link","img","input","br","hr","source"}
    def __init__(self):
        super().__init__()
        self.stack=[]
        self.elements=[]
        self.byid={}
        self.duplicates=[]
    def handle_starttag(self,tag,attributes):
        a=dict(attributes)
        item={"tag":tag,"attrs":a,"ancestors":self.stack[:]}
        self.elements.append(item)
        if "id" in a:
            if a["id"] in self.byid:self.duplicates.append(a["id"])
            self.byid[a["id"]]=item
        if tag not in self.VOID:self.stack.append(item)
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index]["tag"]==tag:
                del self.stack[index:]
                return


class MinimalDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dom=DOM();cls.dom.feed(HTML)

    def test_one_default_page_and_five_distinct_views(self):
        self.assertEqual(self.dom.duplicates,[])
        for name in ("devices","connect","usage","apps","settings"):
            view=self.dom.byid["view-"+name]
            self.assertEqual(view["tag"],"section")
            self.assertEqual("hidden" in view["attrs"],name!="devices")
            self.assertEqual(self.dom.byid["nav-"+name]["tag"],"a")
        self.assertIn('id="panel"',HTML)
        self.assertNotIn('id="auth-form"',HTML)
        self.assertNotIn('id="auth-password"',HTML)

    def test_device_home_contains_no_onboarding_or_diagnostics(self):
        in_view=lambda item,view: any(a["attrs"].get("id")=="view-"+view
                                       for a in self.dom.byid[item]["ancestors"])
        for k in ("devices","pair","devices-total","refresh","open-apps"):
            self.assertTrue(in_view(k,"devices"),k)
        for k in ("quick-installer","manage-agent","install-deps-command"):
            self.assertTrue(in_view(k,"connect"),k)
            self.assertFalse(in_view(k,"devices"),k)
        for k in ("usage","errors-section","total-calls","total-errors","goto-errors"):
            self.assertTrue(in_view(k,"usage"),k)
        for k in ("endpoint","oauth-connections","oauth-refresh","mcp-example"):
            self.assertTrue(in_view(k,"apps"),k)
        for k in ("diagnostic","email","userbar","admin-panel"):
            self.assertTrue(in_view(k,"settings"),k)

    def test_advanced_onboarding_not_visible_by_default(self):
        for ident in ("manage-agent","install-advanced"):
            element=self.dom.byid[ident]
            self.assertEqual(element["tag"],"details")
            self.assertNotIn("open",element["attrs"])
        self.assertIn("install-pair-display",self.dom.byid)
        self.assertIn("hidden",self.dom.byid["install-pair-display"]["attrs"])
        self.assertIn("install-copy-pair",self.dom.byid)

    def test_no_stored_secrets_or_fake_backend_api(self):
        for required in ("'/api/devices/revoke'","'/api/oauth/connections/revoke'",
                         "'/api/account/diagnostics'","'/api/me'",
                         "window.addEventListener('hashchange'"):
            self.assertIn(required,JS)
        self.assertIn("textContent=d.name",JS)
        self.assertIn("textContent=client.client_name",JS)
        self.assertIn("if(currentView==='apps')",JS)
        self.assertIn("id=\"panel-notice\"",HTML)
        self.assertNotIn("innerHTML=",JS)

    def test_responsive_compact_layout_and_accessibility(self):
        self.assertIn("@media(max-width:700px)",CSS)
        self.assertIn(".hub-app .hub-navigation{display:flex;",CSS)
        self.assertIn("button:focus-visible",CSS)
        self.assertIn("aria-label=\"Главная навигация\"",HTML)
        self.assertIn("role=\"status\"",HTML)
        self.assertIn("class=\"hub-sidebar\"",HTML)
        self.assertIn("class=\"hub-content\"",HTML)


if __name__=="__main__":
    unittest.main()
