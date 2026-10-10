"""A safety check must catch sensitive literals in tests and in itself."""
import sys
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_public_tree import private_literal_findings, check_public_tree


class PublicTreePrivacyTests(unittest.TestCase):
    def test_non_example_email_detected(self):
        address = "person" + "@" + "private" + "." + "ru"
        self.assertIn("non-example email address", private_literal_findings(address))

    def test_operational_host_detected_without_full_url(self):
        host = "remote" + "." + "internal" + "." + "ru"
        self.assertIn("non-example bare hostname", private_literal_findings(host))

    def test_url_is_checked(self):
        uri = "https://" + "host.private" + ".ru" + "/mcp"
        self.assertIn("non-allowlisted network origin", private_literal_findings(uri))

    def test_safe_examples_and_official_links(self):
        examples = ("https://hub.example.com/mcp owner@example.test "
                    "https://github.com/owner/repo "
                    "https://chatgpt.com")
        self.assertFalse(private_literal_findings(examples))

    def test_external_blocklist(self):
        synthetic = "private-info-marker-123"
        self.assertIn("private identifier in external blocklist",
                      private_literal_findings(synthetic, (synthetic,)))

    def test_bare_domains_without_protocol_are_blocked(self):
        # Assemble the examples so the tests do not themselves contain real
        # domain literals. CI checks the test source, too.
        domain = "private-hub" + "." + "company" + ".com"
        finding = private_literal_findings("MCP=" + domain)
        self.assertIn("non-example bare hostname", finding)
        alt = "private-api" + "." + "service" + ".dev"
        self.assertIn("non-example bare hostname",
                      private_literal_findings("host: " + alt))

    def test_private_dns_and_ip_literals_are_blocked(self):
        internal = "office" + "." + "company" + ".local"
        private_ip = ".".join(map(str, [10, 12, 20, 30]))
        self.assertIn("non-example bare hostname",
                      private_literal_findings("MCP_HOST=" + internal))
        self.assertIn("non-documentation private network address",
                      private_literal_findings(private_ip))
        self.assertNotIn("non-documentation private network address",
                         private_literal_findings("192.0.2.123"))

    def test_code_attributes_are_not_misread_as_domains(self):
        fragment = "Path.home() os.name params.name data.online_devices"
        self.assertFalse(private_literal_findings(fragment))

    def test_release_guard_scans_created_tracked_test_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            tmp = Path(workspace)
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            target = tmp / "tests" / "test_private.py"
            target.parent.mkdir(parents=True)
            domain = "personal-service" + "." + "company" + ".com"
            target.write_text("endpoint = " + repr(domain) + "\n",
                              encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, check=True)
            findings = check_public_tree(tmp)
            self.assertTrue(any("test_private.py" in finding and
                                "bare hostname" in finding for finding in findings))

    def test_external_blocklist_even_when_no_hostname(self):
        synthetic = "synthetic-private-marker"
        self.assertIn("private identifier in external blocklist",
                      private_literal_findings("x = " + synthetic,
                                               (synthetic,)))

    def test_scans_full_tracked_tree_including_tests(self):
        self.assertEqual(check_public_tree(ROOT), [])
