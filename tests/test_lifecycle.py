"""Safe local unpair/uninstall behavior and tenant-bound server revocation."""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'install'))
sys.path.insert(0,str(ROOT/'server'))
import manage_agent as manager
from hub import Hub,Failure
from distribution import CLI,AGENT,build_distributions


class LocalLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name)
        self.home=base/'tester'
        self.home.mkdir()
        self.root=self.home/'.asc-remote-hub'/'agent'
        self.root.mkdir(parents=True)
        self.confdir=self.home/'.config'/'asc-remote-hub'
        self.confdir.mkdir(parents=True)
        self.config=self.confdir/'agent.json'
        self.secret=self.confdir/'device-key.secret'
        self.secret.write_text('token-'+'x'*48,encoding='utf8')
        self.did='d_'+'1'*32
        self.config.write_text(json.dumps({
          'hub_url':'https://hub.example.test','device_id':self.did,
          'token_file':str(self.secret),'name':'fixture'}),encoding='utf8')
        (self.root/'keep.py').write_text('test',encoding='utf8')

    def test_remote_revocation_with_exact_identity(self):
        with patch.object(manager,'request',return_value={'revoked':True,'device_id':self.did}) as req:
            manager.revoke_pairing(self.config,local_only=False)
        self.assertEqual(req.call_args.args[1],'/agent/unpair')
        self.assertEqual(req.call_args.args[2],{'confirm_device_id':self.did})
        self.assertEqual(req.call_args.args[4],self.did)

    def test_unavailable_hub_preserves_credentials(self):
        with patch.object(manager,'request',side_effect=URLError('offline')):
            with self.assertRaisesRegex(RuntimeError,'недоступен'):
                manager.revoke_pairing(self.config,local_only=False)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.secret.exists())

    def test_local_only_skips_server(self):
        with patch.object(manager,'request') as req:
            manager.revoke_pairing(self.config,local_only=True)
            req.assert_not_called()

    def test_local_cleanup_removes_only_credentials(self):
        other=self.confdir/'unrelated.txt'
        other.write_text('preserve',encoding='utf8')
        manager.cleanup_pairing(self.config,None)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.secret.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.root/'keep.py').exists())

    def test_config_redirect_is_rejected(self):
        self.config.write_text(json.dumps({
          'hub_url':'https://hub.example.test','device_id':self.did,
          'token_file':str(self.home/'foreign-key')}),encoding='utf8')
        with self.assertRaisesRegex(RuntimeError,'Unexpected credential'):
            manager.cleanup_pairing(self.config,None)
        self.assertTrue(self.config.exists())

    def test_bogus_linux_systemd_unit_refused(self):
        if os.name=='nt':self.skipTest('Linux fixture')
        # Tested with patched os.geteuid and Path.home() where supported.
        task=self.home/'.config'/'systemd'/'user'/'asc-remote-hub-agent.service'
        task.parent.mkdir(parents=True)
        task.write_text('[Service]\nExecStart=/usr/bin/python3 /tmp/other/bridge.py run\n')
        with patch.object(manager.Path,'home',return_value=self.home), patch.object(manager.os,'geteuid',return_value=1001):
            with self.assertRaisesRegex(RuntimeError,'not this ASC agent'):
                manager.verified_linux_unit(self.root)


class ServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub=Hub(base_url='http://127.0.0.1:9010',data_dir=self.tmp.name,test_mode=True)
        self.u1=self.hub.register_user('lifecycle1@example.test','Password-Unpair-1234')
        self.u2=self.hub.register_user('lifecycle2@example.test','Password-Unpair-5678')
        def pair(u):
            key=self.hub.pairing_create(u['id'])
            return self.hub.pairing_redeem(key,'Fixture','Linux',['machine_info'])
        self.d1=pair(self.u1)
        self.d2=pair(self.u2)

    def test_revoke_one_device_only(self):
        identity=self.hub.agent_identity(self.d1['device_id'],self.d1['device_token'])
        reply=self.hub.device_revoke(identity)
        self.assertTrue(reply['revoked'])
        with self.assertRaises(Failure):
            self.hub.agent_identity(self.d1['device_id'],self.d1['device_token'])
        self.assertIsNotNone(self.hub.agent_identity(self.d2['device_id'],self.d2['device_token']))
        self.assertEqual(len(self.hub.list_devices(self.u1['id'])),0)
        self.assertEqual(len(self.hub.list_devices(self.u2['id'])),1)

    def test_public_archive_has_manager_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            dist=build_distributions(ROOT,Path(temp),'https://other-hub.example.test')
            with tarfile.open(dist[AGENT],mode='r:gz') as tar:
                files=tar.getnames()
                self.assertIn('asc-remote-hub/install/manage_agent.py',files)
                self.assertNotIn('asc-remote-hub/.env',files)
            with tarfile.open(dist[CLI],mode='r:gz') as tar:
                cli=tar.extractfile('package/bin/connect.cjs').read().decode('utf8')
                self.assertIn('unpair',cli)
                self.assertIn('uninstall',cli)
                self.assertIn('other-hub.example.test',cli)


if __name__=='__main__':
    unittest.main()
