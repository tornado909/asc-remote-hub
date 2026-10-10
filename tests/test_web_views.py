"""The homepage is selected on the server using a validated session cookie.

Regression for login forms flashing and remaining in authenticated page source.
"""
import http.cookiejar
import json
from html.parser import HTMLParser
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
from hub import Hub,Handler,ThreadingHTTPServer,digest

class DOMIDs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids=set()
    def handle_starttag(self,tag,attrs):
        ident=dict(attrs).get("id")
        if ident:self.ids.add(ident)

def ids(document):
    parser=DOMIDs()
    parser.feed(document)
    return parser.ids

class ServerViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
        sock.close()
        self.url="http://127.0.0.1:"+str(port)
        self.hub=Hub(base_url=self.url,data_dir=self.temp.name,test_mode=True)
        self.owner=self.hub.register_user("person@example.test","Test-Password-2026-Strong")
        self.server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        self.server.hub=self.hub
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def load(self,url="/",cookie=None):
        headers={"Cookie":"asc_sid="+cookie} if cookie else {}
        with urlopen(Request(self.url+url,headers=headers),timeout=8) as response:
            return response.read().decode("utf-8"),dict(response.headers)

    def test_guest_home_has_only_login_not_dashboard(self):
        page,headers=self.load()
        self.assertIn("auth-form",ids(page))
        self.assertIn("auth-password",ids(page))
        self.assertIn("quick-installer",ids(page))
        self.assertNotIn("panel",ids(page))
        self.assertNotIn("errors-section",ids(page))
        self.assertNotIn("devices",ids(page))
        self.assertIn('src="/static/auth.js"',page)
        self.assertNotIn('src="/static/dashboard.js"',page)
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(headers["Vary"],"Cookie")

    def test_member_html_has_no_auth_form_in_response_bytes(self):
        sid,csrf=self.hub.session_create(self.owner["id"])
        page,headers=self.load(cookie=sid)
        self.assertNotIn("auth-form",page)
        self.assertNotIn("auth-email",page)
        self.assertNotIn("auth-password",page)
        self.assertNotIn('id="welcome"',page)
        self.assertNotIn("Регистрация</button>",page)
        self.assertNotIn('src="/static/auth.js"',page)
        self.assertIn('src="/static/dashboard.js"',page)
        self.assertIn("panel",ids(page))
        self.assertIn("errors-section",ids(page))
        self.assertIn("quick-installer",ids(page))
        self.assertIn("install-create-pair",ids(page))
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(headers["Vary"],"Cookie")

    def test_expired_or_invalid_cookie_cannot_show_dashboard(self):
        self.assertIn("auth-form",ids(self.load(cookie="x"*64)[0]))
        sid,_=self.hub.session_create(self.owner["id"])
        self.assertIn("panel",ids(self.load(cookie=sid)[0]))
        self.hub.execute("UPDATE sessions SET expires=? WHERE hash=?",
                         (time.time()-10,digest(sid)))
        self.assertIn("auth-form",ids(self.load(cookie=sid)[0]))
        self.assertNotIn("panel",ids(self.load(cookie=sid)[0]))

    def test_register_login_logout_roundtrip(self):
        browser=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def post(route,body,csrf=""):
            req=Request(self.url+route,data=json.dumps(body).encode(),method="POST",
                        headers={"Content-Type":"application/json","X-ASC-CSRF":csrf})
            with browser.open(req,timeout=12) as response:
                return json.loads(response.read().decode())
        with browser.open(self.url+"/",timeout=8) as response:
            self.assertIn(b'id="auth-form"',response.read())
        with self.assertRaises(HTTPError) as error:
            post("/api/login",{"email":"person@example.test","password":"not-my-password"})
        self.assertEqual(error.exception.code,401)
        logged=post("/api/login",{"email":"person@example.test",
                                  "password":"Test-Password-2026-Strong"})
        self.assertTrue(logged["ok"])
        with browser.open(self.url+"/",timeout=8) as response:
            private=response.read()
            self.assertIn(b'id="panel"',private)
            self.assertNotIn(b'id="auth-form"',private)
        with browser.open(self.url+"/api/devices",timeout=8) as response:
            self.assertIn("devices",json.loads(response.read()))
        self.assertTrue(post("/api/logout",{},csrf=logged["csrf"])["ok"])
        with browser.open(self.url+"/",timeout=8) as response:
            public=response.read()
            self.assertIn(b'id="auth-form"',public)
            self.assertNotIn(b'id="panel"',public)
        with self.assertRaises(HTTPError) as error:
            browser.open(self.url+"/api/devices",timeout=8)
        self.assertEqual(error.exception.code,401)

    def test_only_view_specific_scripts_are_requested(self):
        for path,keyword in (("/static/common.js","INSTALLER_URL"),
                             ("/static/auth.js","auth-form"),
                             ("/static/dashboard.js","refreshErrors")):
            content,headers=self.load(path)
            self.assertIn(keyword,content)
            self.assertEqual(headers["Cache-Control"],"no-store")
        with self.assertRaises(HTTPError) as error:
            self.load("/static/app.js")
        self.assertEqual(error.exception.code,404)
