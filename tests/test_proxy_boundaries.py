"""Proxy trust boundaries and public URL invariants."""
import ipaddress
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
from hub import Hub,Handler

class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ,{"ASC_HUB_TRUSTED_PROXIES":""}):
            self.hub=Hub(base_url="https://hub.example.com",data_dir=self.temp.name,test_mode=True)
        self.handler=object.__new__(Handler)
        self.handler.server=types.SimpleNamespace(hub=self.hub)
        self.handler.client_address=("192.0.2.5",44444)
        self.handler.headers={"X-Real-IP":"198.51.100.90"}

    def test_untrusted_header_is_ignored(self):
        self.assertEqual(self.handler.client_ip,"192.0.2.5")
        self.handler.headers["X-Real-IP"]="not-an-ip"
        self.assertEqual(self.handler.client_ip,"192.0.2.5")

    def test_only_trusted_proxy_uses_valid_header(self):
        self.hub.trusted_proxies=[ipaddress.ip_network("192.0.2.5/32")]
        self.assertEqual(self.handler.client_ip,"198.51.100.90")
        self.handler.headers["X-Real-IP"]="bad"
        self.assertEqual(self.handler.client_ip,"192.0.2.5")
        self.handler.headers["X-Real-IP"]="198.51.100.18,127.0.0.1"
        self.assertEqual(self.handler.client_ip,"192.0.2.5")

    def test_env_cidr_parsing(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ,{"ASC_HUB_TRUSTED_PROXIES":"192.0.2.0/24,127.0.0.1/32"}):
                hub=Hub(base_url="https://another.example.org",data_dir=folder,test_mode=True)
            self.assertEqual(len(hub.trusted_proxies),2)
            self.assertIn(ipaddress.ip_address("192.0.2.123"),hub.trusted_proxies[0])

    def test_different_hub_url_does_not_leak_other_origin(self):
        package=self.hub.public_packages["cli-1.0.0.tgz"]
        import tarfile
        with tarfile.open(package,"r:gz") as archive:
            src=archive.extractfile("package/bin/connect.cjs").read().decode()
            self.assertIn("https://hub.example.com",src)
            self.assertNotIn("private.example.invalid",src)
