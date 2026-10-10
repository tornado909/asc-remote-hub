"""Public installer endpoints and domain portability."""
import hashlib
from html.parser import HTMLParser
import io
from http.server import ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import tarfile
import tempfile
import threading
import unittest
from urllib.request import urlopen

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"server"))
from hub import Hub,Handler
from distribution import CLI,AGENT,build_distributions

class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids=set()
        self.duplicates=set()
    def handle_starttag(self,tag,attrs):
        fields=dict(attrs)
        if "id" in fields:
            if fields["id"] in self.ids:self.duplicates.add(fields["id"])
            self.ids.add(fields["id"])

class InstallerAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
        sock.close()
        cls.hub=Hub(base_url=f"http://127.0.0.1:{port}",data_dir=cls.tmp.name,test_mode=True)
        cls.server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        cls.server.hub=cls.hub
        cls.server.daemon_threads=True
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f"http://127.0.0.1:{port}"
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(4)
        cls.tmp.cleanup()

    def test_binary_urls_match_generated_assets(self):
        for uri,name in (("/cli.tgz",CLI),("/"+CLI,CLI),("/downloads/"+AGENT,AGENT)):
            expected=self.hub.public_packages[name].read_bytes()
            with urlopen(self.url+uri,timeout=8) as response:
                self.assertEqual(response.status,200)
                self.assertEqual(response.headers.get("Content-Type"),"application/gzip")
                self.assertEqual(response.read(),expected)
            self.assertGreater(len(expected),2000)

    def test_public_site_contains_login_not_private_dashboard(self):
        with urlopen(self.url+"/",timeout=8) as response:
            page=response.read().decode("utf8")
            self.assertEqual(response.headers.get("Cache-Control"),"no-store")
            self.assertEqual(response.headers.get("Vary"),"Cookie")
        tags=Tags()
        tags.feed(page)
        self.assertFalse(tags.duplicates)
        for id in ("quick-installer","install-command","install-copy","install-win",
                   "install-linux","auth-form","auth-password"):
            self.assertIn(id,tags.ids)
        for id in ("panel","errors-section","errors-header-count","devices","admin-panel"):
            self.assertNotIn(id,tags.ids)
        self.assertIn(AGENT,page)
        self.assertNotIn("private.example.invalid",page)
        self.assertNotIn("private-account@example.invalid",page)
        self.assertIn('src="/static/auth.js"',page)
        self.assertNotIn('src="/static/dashboard.js"',page)
        with urlopen(self.url+"/static/common.js",timeout=8) as response:
            js=response.read().decode("utf8")
        self.assertIn("location.origin+'/cli-1.0.0.tgz'",js)
        self.assertIn("copyInstallValue",js)
        self.assertNotIn("private.example.invalid",js)

    def test_same_release_multiple_domains_produce_distinct_clis(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            aa=build_distributions(HERE,Path(a),"https://hub-one.example.net")
            bb=build_distributions(HERE,Path(b),"https://another.example.org:8443")
            self.assertEqual(aa[AGENT].read_bytes(),bb[AGENT].read_bytes())
            self.assertNotEqual(aa[CLI].read_bytes(),bb[CLI].read_bytes())
            for origin,content in (("https://hub-one.example.net",aa[CLI].read_bytes()),
                                   ("https://another.example.org:8443",bb[CLI].read_bytes())):
                with tarfile.open(fileobj=io.BytesIO(content),mode="r:gz") as tar:
                    code=tar.extractfile("package/bin/connect.cjs").read().decode("utf8")
                    self.assertIn(origin,code)
                    self.assertNotIn("private.example.invalid",code)
                    self.assertNotIn("__HUB_ORIGIN__",code)
                    self.assertNotIn("__ARCHIVE_SHA256__",code)
            with tarfile.open(aa[AGENT],mode="r:gz") as tar:
                names=tar.getnames()
                self.assertTrue(all(x.startswith("asc-remote-hub/") for x in names))
                self.assertFalse(any(".env" in x or "sqlite" in x or "backup" in x for x in names))
                self.assertEqual(len(names),7)

    def test_health_and_oauth_still_work(self):
        for route in ("/healthz","/.well-known/oauth-authorization-server"):
            with urlopen(self.url+route,timeout=8) as response:
                self.assertEqual(response.status,200)
