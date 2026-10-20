import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from hub import Hub,pkce

class AppConnectionManagementTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.h=Hub(base_url='http://127.0.0.1:9099',data_dir=t.name,test_mode=True)
        self.p='StrongPassword2026!'
        self.a=self.h.register_user('owner@example.org',self.p)
        self.b=self.h.register_user('second@example.org',self.p)
        self.redirect='http://localhost:8787/callback'
        self.client=self.h.oauth_register({
            'redirect_uris':[self.redirect],'client_name':'Cursor'})['client_id']
    def grant(self,email):
        verifier='v'*64
        self.h.authorize({
            'client_id':self.client,'redirect_uri':self.redirect,
            'response_type':'code','code_challenge_method':'S256',
            'code_challenge':pkce(verifier),'scope':'remote.access offline_access'
        })
        ticket=self.h.row('SELECT id FROM oauth_requests ORDER BY rowid DESC LIMIT 1')['id']
        dest=self.h.oauth_approve({'ticket':ticket,'consent':'yes',
                              'email':email,'password':self.p},'127.0.0.1')
        code=parse_qs(urlparse(dest).query)['code'][0]
        return self.h.oauth_token({
            'grant_type':'authorization_code','client_id':self.client,
            'redirect_uri':self.redirect,'code':code,'code_verifier':verifier
        })
    def test_connections_separate_tenants_and_revoke_only_own_tokens(self):
        a=self.grant('owner@example.org')
        b=self.grant('second@example.org')
        one=self.h.oauth_connections(self.a['id'])
        two=self.h.oauth_connections(self.b['id'])
        self.assertEqual([c['client_name'] for c in one['connections']],['Cursor'])
        self.assertEqual(one['connections'][0]['callback_hosts'],['localhost:8787'])
        self.assertEqual(len(two['connections']),1)
        self.assertTrue(self.h.oauth_revoke_connection(self.a['id'],self.client))
        self.assertIsNone(self.h.oauth_owner(a['access_token']))
        self.assertIsNotNone(self.h.oauth_owner(b['access_token']))
        self.assertEqual(self.h.oauth_connections(self.a['id'])['connections'],[])
        self.assertEqual(len(self.h.oauth_connections(self.b['id'])['connections']),1)
        self.assertFalse(self.h.oauth_revoke_connection(self.a['id'],self.client))
        # No cross-tenant deletion even when client_id is known.
        self.assertFalse(self.h.oauth_revoke_connection(self.a['id'],'c_unknown'))
        self.assertIsNotNone(self.h.oauth_owner(b['access_token']))
        self.assertFalse(self.h.row(
            'SELECT hash FROM oauth_tokens WHERE user_id=?',(self.a['id'],)))
    def test_users_without_connections_only_see_their_own(self):
        self.grant('owner@example.org')
        self.assertEqual(self.h.oauth_connections(self.b['id'])['connections'],[])

if __name__=='__main__':
    unittest.main()
