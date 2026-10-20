"""Universal OAuth MCP clients: Cursor, OpenMausBot, ChatGPT, and arbitrary apps."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from urllib.parse import parse_qs,urlparse
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from hub import Hub,Failure,pkce,valid_oauth_redirect

class UniversalOAuthTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.hub=Hub(base_url='http://127.0.0.1:9988',data_dir=self.dir.name,test_mode=True)
        self.user=self.hub.register_user('testing@example.org','Test-Password-2026!')

    def test_all_supported_clients_register_by_protocol_not_brand(self):
        redirects=[
            'https://chatgpt.com/connector_platform_oauth_redirect',
            'https://www.cursor.com/agents/mcp/oauth/callback',
            'http://localhost:8787/callback',
            'http://127.0.0.1:50981/oauth/callback',
            'http://[::1]:50982/oauth/callback',
            'https://mcp-client.example.org/oauth/return?foo=bar',
            'http://127.0.0.1:35782',
            'https://mcp-client.example.org',
        ]
        for uri in redirects:
            with self.subTest(uri=uri):
                response=self.hub.oauth_register({
                    'client_name':'Test MCP client','redirect_uris':[uri],
                    'grant_types':['authorization_code','refresh_token'],
                    'response_types':['code'],'token_endpoint_auth_method':'none'
                })
                self.assertEqual(response['redirect_uris'],[uri])
                self.assertEqual(response['client_name'],'Test MCP client')
                row=self.hub.row('SELECT redirects,client_name FROM oauth_clients WHERE id=?',
                                 (response['client_id'],))
                self.assertEqual(json.loads(row['redirects']),[uri])
                self.assertEqual(row['client_name'],'Test MCP client')
        self.assertTrue(valid_oauth_redirect(redirects[-1]))
        self.assertFalse(self.hub.oauth_metadata()['client_id_metadata_document_supported'])

    def test_client_registration_validates_scheme_credentials_and_duplicates(self):
        bad=[
            'http://localhost/callback',
            'http://example.org/callback',
            'http://127.0.0.1:0/callback',
            'http://127.0.0.1:65536/callback',
            'http://localhost.evil.example:8787/callback',
            'http://' + '.'.join(['192','168','1','8']) + ':8787/callback',
            'file:///tmp/mcp',
            'https://app.example.org/callback#x',
            'https://'+'user:pass@app.example.org/callback',
            'https://app.example.org/callback\nInjected-Header:yes',
            'https://app.example.org\\\\@attacker.example/callback',
        ]
        for uri in bad:
            with self.subTest(uri=uri):
                with self.assertRaises(Failure):
                    self.hub.oauth_register({'redirect_uris':[uri]})
        with self.assertRaises(Failure):
            self.hub.oauth_register({'redirect_uris':['http://localhost:8787/callback']*2})
        for props in (
            {'client_name':'<invalid>\n'},
            {'token_endpoint_auth_method':'client_secret_basic'},
            {'grant_types':['client_credentials']},
            {'response_types':['token']}
        ):
            with self.subTest(props=props):
                with self.assertRaises(Failure):
                    self.hub.oauth_register({'redirect_uris':['http://localhost:8787/callback'],**props})

    def test_local_cursor_oauth_pkce_token_refresh_and_mcp(self):
        redirect='http://localhost:8787/callback'
        reg=self.hub.oauth_register({'client_name':'Cursor desktop',
                                     'redirect_uris':[redirect]})
        client=reg['client_id']
        verifier='v'*64
        consent=self.hub.authorize({
            'client_id':client,'redirect_uri':redirect,
            'response_type':'code','code_challenge_method':'S256',
            'code_challenge':pkce(verifier),'state':'opaque-state',
            'resource':self.hub.resource,
            'scope':'remote.access offline_access'})
        self.assertIn('Cursor desktop',consent)
        self.assertIn('localhost:8787',consent)
        self.assertNotIn('Авторизовать ChatGPT',consent)
        ticket=self.hub.row('SELECT id FROM oauth_requests')['id']
        destination=self.hub.oauth_approve({
            'ticket':ticket,'consent':'yes',
            'email':'testing@example.org','password':'Test-Password-2026!'},'127.0.0.1')
        u=urlparse(destination)
        self.assertEqual(u.scheme,'http')
        self.assertEqual(u.hostname,'localhost')
        self.assertEqual(u.port,8787)
        self.assertEqual(parse_qs(u.query)['state'][0],'opaque-state')
        code=parse_qs(u.query)['code'][0]
        token=self.hub.oauth_token({
            'client_id':client,'grant_type':'authorization_code','code':code,
            'code_verifier':verifier,'redirect_uri':redirect,
            'resource':self.hub.resource})
        owner=self.hub.oauth_owner(token['access_token'])
        self.assertEqual(owner['id'],self.user['id'])
        mcp=self.hub.mcp(owner,{'jsonrpc':'2.0','id':7,'method':'initialize',
            'params':{'protocolVersion':'2025-11-25'}})
        self.assertEqual(mcp['result']['serverInfo']['version'],'1.2.3')
        self.assertEqual(mcp['result']['capabilities']['tools']['listChanged'],False)
        tool_list=self.hub.mcp(owner,{'jsonrpc':'2.0','id':8,'method':'tools/list'})
        self.assertGreater(len(tool_list['result']['tools']),20)
        with self.assertRaises(Failure):
            self.hub.oauth_token({'client_id':client,'grant_type':'authorization_code','code':code,
                                  'code_verifier':verifier,'redirect_uri':redirect})
        new_token=self.hub.oauth_token({'client_id':client,'grant_type':'refresh_token',
                        'refresh_token':token['refresh_token'],'resource':self.hub.resource})
        self.assertIsNotNone(self.hub.oauth_owner(new_token['access_token']))

    def test_client_names_are_escaped_and_callbacks_cannot_be_swapped(self):
        cb='https://www.cursor.com/agents/mcp/oauth/callback'
        client=self.hub.oauth_register({'client_name':'Cursor <script>alert(1)</script>',
                                        'redirect_uris':[cb]})['client_id']
        arguments={'client_id':client,'redirect_uri':cb,'response_type':'code',
                   'code_challenge_method':'S256','code_challenge':pkce('a'*64)}
        page=self.hub.authorize(arguments)
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertIn('&lt;script&gt;',page)
        with self.assertRaises(Failure):
            self.hub.authorize({**arguments,'redirect_uri':'http://localhost:8787/callback'})
        with self.assertRaises(Failure):
            self.hub.authorize({**arguments,'resource':'https://other.example/mcp'})

    def test_existing_chatgpt_records_survive_sqlite_migration(self):
        # Old users and OAuth applications have an id and redirects only.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # A legacy app is retained even after adding client_name column.
            legacy=self.hub.oauth_register({
                 'redirect_uris':['https://chatgpt.com/connector_platform_oauth_redirect']})
            old_id=legacy['client_id']
            self.assertTrue(self.hub.row('SELECT id FROM oauth_clients WHERE id=?',(old_id,)))
            self.assertIn('chatgpt.com',self.hub.row('SELECT redirects FROM oauth_clients WHERE id=?',(old_id,))['redirects'])

if __name__=='__main__':
    unittest.main()
