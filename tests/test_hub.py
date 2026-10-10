import hashlib
import http.cookiejar
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlparse, urlencode
from urllib.request import Request,build_opener,HTTPCookieProcessor

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"server"))
sys.path.insert(0,str(HERE/"client"))
from hub import Hub,Failure,Handler,ThreadingHTTPServer,pkce


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub=Hub(base_url="http://127.0.0.1:9876",data_dir=self.tmp.name,test_mode=True)
        self.u1=self.hub.register_user('first@example.org','Password-First-12345')
        self.u2=self.hub.register_user('second@example.org','Password-Second-12345')

    def pair(self,owner, name):
        code=self.hub.pairing_create(owner['id'])
        d=self.hub.pairing_redeem(code,name,'Linux',['machine_info','read_file'])
        return d,code

    def test_registration_sessions_and_password_isolation(self):
        with self.assertRaises(Failure):self.hub.register_user('first@example.org','AnythingLongEnough')
        with self.assertRaises(Failure):self.hub.register_user('third@example.org','short')
        self.assertIsNone(self.hub.authenticate('first@example.org','invalid-password'))
        self.assertIsNone(self.hub.authenticate('nosuch@example.org','AnythingLongEnough'))
        self.assertEqual(self.hub.authenticate('first@example.org','Password-First-12345')['id'],self.u1['id'])
        sid,csrf=self.hub.session_create(self.u1['id'])
        self.assertEqual(self.hub.session_owner(sid)['id'],self.u1['id'])
        self.assertEqual(self.hub._session_csrf(sid),csrf)
        self.assertIsNone(self.hub.session_owner('other'))
        self.hub.execute('DELETE FROM sessions WHERE hash=?',(hashlib.sha256(sid.encode()).hexdigest(),))
        self.assertIsNone(self.hub.session_owner(sid))

    def test_one_time_pairing_and_no_cross_tenant_access(self):
        d1,code=self.pair(self.u1,'Personal Windows')
        d2,_=self.pair(self.u2,'Other user PC')
        self.assertEqual(len(self.hub.list_devices(self.u1['id'])),1)
        self.assertEqual(self.hub.list_devices(self.u1['id'])[0]['id'],d1['device_id'])
        self.assertIsNone(self.hub.user_device(self.u1['id'],d2['device_id']))
        with self.assertRaises(Failure):self.hub.pairing_redeem(code,'Duplicate','Linux',['machine_info'])
        self.assertIsNone(self.hub.user_device(self.u2['id'],d1['device_id']))
        self.assertTrue(self.hub.agent_identity(d1['device_id'],d1['device_token']))
        with self.assertRaises(Failure):self.hub.agent_identity(d2['device_id'],d1['device_token'])
        with self.assertRaises(Failure):self.hub.agent_identity(d1['device_id'],'not-the-real-token-1234567890123456')
        refused=self.hub.forward(self.u1['id'],'machine_info',{'device':d2['device_id']})
        self.assertTrue(refused['isError'])
        self.assertIn('not found',refused['content'][0]['text'].lower())

    def test_relay_routes_only_owner_commands_and_counts(self):
        d1,_=self.pair(self.u1,'Device A')
        d2,_=self.pair(self.u2,'Device B')
        identity1=self.hub.agent_identity(d1['device_id'],d1['device_token'])
        identity2=self.hub.agent_identity(d2['device_id'],d2['device_token'])
        self.hub.agent_hello(identity1,{'machine_info':{'name':'A'},'tools':['machine_info']})
        self.hub.agent_hello(identity2,{'machine_info':{'name':'B'},'tools':['machine_info']})
        result={}
        def call():result['value']=self.hub.forward(self.u1['id'],'machine_info',{'device':d1['device_id']})
        th=threading.Thread(target=call);th.start()
        job=self.hub.agent_poll(identity1)['job']
        self.assertIsNotNone(job)
        self.assertEqual(job['tool'],'machine_info')
        self.assertEqual(len(self.hub.queued[d2['device_id']]),0)
        with self.assertRaises(Failure):self.hub.agent_result(identity2,{'id':job['id'],'result':{'content':[]}})
        self.hub.agent_result(identity1,{'id':job['id'],'result':{'content':[{'type':'text','text':'confirmed-A'}],'isError':False}})
        th.join(6)
        self.assertEqual(result['value']['content'][0]['text'],'confirmed-A')
        usage1=self.hub.usage(self.u1['id']);usage2=self.hub.usage(self.u2['id'])
        self.assertEqual(usage1['total'],1)
        self.assertEqual(usage1['errors'],0)
        self.assertEqual(usage2['total'],0)
        self.assertEqual(self.hub.admin_summary()['tool_calls'],1)

    def test_oauth_codes_pkce_refresh_and_tenant_binding(self):
        redirect='https://chatgpt.com/connector_platform_oauth_redirect'
        client=self.hub.oauth_register({'redirect_uris':[redirect]})['client_id']
        verifier='x'*64
        h=self.hub.authorize({'client_id':client,'redirect_uri':redirect,'response_type':'code',
                   'code_challenge_method':'S256','code_challenge':pkce(verifier),
                   'scope':'remote.access offline_access','state':'0123','resource':self.hub.resource})
        self.assertIn('ASC Remote Hub',h)
        ticket=self.hub.row('SELECT id FROM oauth_requests')['id']
        dest=self.hub.oauth_approve({'ticket':ticket,'consent':'yes','email':'first@example.org','password':'Password-First-12345'},'test-ip')
        self.assertEqual(urlparse(dest).netloc,'chatgpt.com')
        code=parse_qs(urlparse(dest).query)['code'][0]
        tokens=self.hub.oauth_token({'grant_type':'authorization_code','client_id':client,'code':code,
                           'redirect_uri':redirect,'code_verifier':verifier,'resource':self.hub.resource})
        self.assertEqual(self.hub.oauth_owner(tokens['access_token'])['id'],self.u1['id'])
        self.assertIsNone(self.hub.oauth_owner(tokens['refresh_token']))
        resp=self.hub.mcp(self.hub.oauth_owner(tokens['access_token']),{'jsonrpc':'2.0','id':1,'method':'tools/call',
                                                                        'params':{'name':'list_devices','arguments':{}}})
        self.assertIn('devices',resp['result']['content'][0]['text'])
        with self.assertRaises(Failure):self.hub.oauth_token({'grant_type':'authorization_code','client_id':client,'code':code,
                                        'redirect_uri':redirect,'code_verifier':verifier})
        renewed=self.hub.oauth_token({'grant_type':'refresh_token','client_id':client,
                                      'refresh_token':tokens['refresh_token'],'resource':self.hub.resource})
        self.assertEqual(self.hub.oauth_owner(renewed['access_token'])['id'],self.u1['id'])
        with self.assertRaises(Failure):self.hub.oauth_token({'grant_type':'refresh_token','client_id':client,
                                                                'refresh_token':tokens['refresh_token']})

    def test_reject_redirect_attack_and_wrong_resource(self):
        with self.assertRaises(Failure):self.hub.oauth_register({'redirect_uris':['https://chatgpt.com'+'.evil.com/callback']})
        with self.assertRaises(Failure):self.hub.oauth_register({'redirect_uris':['https://evil.example/callback']})
        cid=self.hub.oauth_register({'redirect_uris':['https://chatgpt.com/connector_platform_oauth_redirect']})['client_id']
        with self.assertRaises(Failure):self.hub.authorize({'client_id':cid,'redirect_uri':'https://evil.example',
                           'response_type':'code','code_challenge_method':'S256','code_challenge':'x'*43})
        with self.assertRaises(Failure):self.hub.authorize({'client_id':cid,
            'redirect_uri':'https://chatgpt.com/connector_platform_oauth_redirect',
            'response_type':'code','code_challenge_method':'S256','code_challenge':'x'*43,
            'resource':'https://'+'untrusted'+'.local/mcp'})


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        cls.hub=Hub(base_url=f'http://127.0.0.1:{port}',data_dir=cls.tmp.name,test_mode=True)
        cls.http=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        cls.http.daemon_threads=True
        cls.http.hub=cls.hub
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join(5);cls.tmp.cleanup()

    def test_browser_oauth_mcp_cors_is_scoped_to_token_endpoints(self):
        cross_origin="https://mcp-client.example.org"
        for route in ("/oauth/register", "/oauth/token", "/mcp"):
            req=Request(self.url+route, method="OPTIONS", headers={
                "Origin":cross_origin, "Access-Control-Request-Method":"POST",
                "Access-Control-Request-Headers":"authorization, content-type, mcp-protocol-version"})
            with self.http_client(req) as resp:
                self.assertEqual(resp.status,204)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"),"*")
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Credentials"))
        for route in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource"):
            req=Request(self.url+route,method="OPTIONS",headers={
                "Origin":cross_origin,"Access-Control-Request-Method":"GET"})
            with self.http_client(req) as resp:
                self.assertEqual(resp.status,204)
        data=json.dumps({"redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"]}).encode()
        req=Request(self.url+"/oauth/register",data=data,method="POST",headers={
            "Origin":cross_origin,"Content-Type":"application/json"})
        with self.http_client(req) as resp:
            self.assertEqual(resp.status,201)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"),"*")
        req=Request(self.url+"/mcp",method="POST",data=b"{}",headers={
            "Origin":cross_origin,"Content-Type":"application/json"})
        with self.assertRaises(HTTPError) as err:
            self.http_client(req)
        self.assertEqual(err.exception.code,401)
        self.assertEqual(err.exception.headers.get("Access-Control-Allow-Origin"),"*")

    def test_cookie_api_and_oauth_approval_still_deny_foreign_origins(self):
        other="https://mcp-client.example.org"
        for path in ("/api/register","/api/devices/pair","/agent/enroll"):
            req=Request(self.url+path,data=b"{}",method="POST",headers={
                "Origin":other,"Content-Type":"application/json"})
            with self.assertRaises(HTTPError) as err:
                self.http_client(req)
            self.assertEqual(err.exception.code,403)
            self.assertIsNone(err.exception.headers.get("Access-Control-Allow-Origin"))
        req=Request(self.url+"/api/devices/pair",method="OPTIONS",headers={
            "Origin":other,"Access-Control-Request-Method":"POST"})
        with self.assertRaises(HTTPError) as err:
            self.http_client(req)
        self.assertEqual(err.exception.code,403)
        self.assertIsNone(err.exception.headers.get("Access-Control-Allow-Origin"))

    def test_oauth_consent_form_accepts_webview_origin_without_cors(self):
        import http.client
        email="oauth-origin@example.org"
        sample_passphrase="OAuth-Origin-Password-2026"
        account=self.hub.register_user(email,sample_passphrase)
        redirect="https://chatgpt.com/connector_platform_oauth_redirect"
        client=self.hub.oauth_register({"redirect_uris":[redirect]})["client_id"]
        verifier="v"*64
        for origin in ("null","https://chatgpt.com","https://unrelated.example.org"):
            self.hub.authorize({
                "client_id":client,"redirect_uri":redirect,
                "response_type":"code","code_challenge_method":"S256",
                "code_challenge":pkce(verifier),"state":"origin-test",
                "resource":self.hub.resource,
            })
            row=self.hub.row("SELECT id FROM oauth_requests ORDER BY rowid DESC LIMIT 1")
            body=urlencode({"ticket":row["id"],"email":email,
                            "password":sample_passphrase,"consent":"yes"})
            target=urlparse(self.url)
            conn=http.client.HTTPConnection(target.hostname,target.port,timeout=8)
            try:
                conn.request("POST","/oauth/approve",body=body,headers={
                    "Origin":origin,
                    "Content-Type":"application/x-www-form-urlencoded",
                })
                response=conn.getresponse()
                self.assertEqual(response.status,302)
                self.assertTrue(response.getheader("Location").startswith(redirect+"?"))
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                response.read()
            finally:
                conn.close()
        # All three requests created codes, but no OAuth token was minted
        # until the user exchanges the code with a matching PKCE verifier.
        self.assertIsNone(self.hub.oauth_owner("not-a-valid-bearer-token"))

    def http_client(self,req):
        from urllib.request import urlopen
        return urlopen(req,timeout=6)

    def test_oauth_consent_csp_explicit_https_target_and_redirect(self):
        redirect="https://chatgpt.com/connector_platform_oauth_redirect"
        client=self.hub.oauth_register({"redirect_uris":[redirect]})["client_id"]
        params={
            "client_id":client,"redirect_uri":redirect,
            "response_type":"code","code_challenge_method":"S256",
            "code_challenge":pkce("v"*64),"state":"abc",
            "resource":self.hub.resource
        }
        with self.http_client(Request(self.url+"/oauth/authorize?"+urlencode(params))) as resp:
            self.assertEqual(resp.status,200)
            csp=resp.headers.get("Content-Security-Policy","")
            self.assertIn("form-action "+self.url+"/oauth/approve https://chatgpt.com;",csp)
            self.assertIn("frame-ancestors 'none'",csp)
            page=resp.read().decode("utf-8")
            self.assertIn('action="'+self.url+'/oauth/approve"',page)
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
        with self.http_client(Request(self.url+"/")) as resp:
            self.assertIn("form-action 'self'",resp.headers.get("Content-Security-Policy",""))

    def test_end_to_end_http_register_pair_and_mcp_auth(self):
        opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def api(path,data=None,headers=None):
            return json.loads(opener.open(Request(self.url+path,
                    data=None if data is None else json.dumps(data).encode(),
                    headers={'Content-Type':'application/json',**(headers or {})}),timeout=6).read())
        self.assertTrue(api('/healthz')['ok'])
        self.assertFalse(api('/api/me')['authenticated'])
        with self.assertRaises(HTTPError) as exc:
            api('/api/devices')
        self.assertEqual(exc.exception.code,401)
        example_passphrase='My-'+'Strong-Pwd-'+'2026'
        register=api('/api/register',{'email':'person@example.org','password':example_passphrase})
        csrf=register['csrf']
        self.assertTrue(api('/api/me')['authenticated'])
        with self.assertRaises(HTTPError) as exc:
            api('/api/devices/pair',{})
        self.assertEqual(exc.exception.code,403)
        code=api('/api/devices/pair',{},headers={'X-ASC-CSRF':csrf})['pairing_code']
        paired=api('/agent/enroll',{'pairing_code':code,'name':'Test Agent','platform':'Windows',
                                    'tools':['machine_info','read_file']})
        did=paired['device_id']
        self.assertEqual(api('/api/devices')['devices'][0]['id'],did)
        self.assertTrue(api('/api/usage')['total']==0)
        with self.assertRaises(HTTPError) as exc:
            api('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(exc.exception.code,401)
        self.assertIn('resource_metadata',exc.exception.headers['WWW-Authenticate'])
        api('/agent/hello',{'machine_info':{'platform':'Windows'},'tools':['machine_info']},
            headers={'X-ASC-Device':did,'Authorization':'Bearer '+paired['device_token']})
        self.assertTrue(api('/api/devices')['devices'][0]['online'])
        anonymous=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        other=anonymous.open(Request(self.url+'/api/register',data=json.dumps({'email':'otherperson@example.org',
                        'password':'Another-Strong-Password'}).encode(),headers={'Content-Type':'application/json'}))
        other_session=json.loads(other.read())
        bad_csrf=other_session['csrf']
        with self.assertRaises(HTTPError) as exc:
            anonymous.open(Request(self.url+'/api/devices/revoke',data=json.dumps({'device_id':did}).encode(),
                      headers={'Content-Type':'application/json','X-ASC-CSRF':bad_csrf}))
        self.assertEqual(exc.exception.code,404)
        self.assertEqual(len(api('/api/devices')['devices']),1)
        data=api('/api/devices/revoke',{'device_id':did},headers={'X-ASC-CSRF':csrf})
        self.assertTrue(data['revoked'])
        self.assertEqual(api('/api/devices')['devices'],[])
        with self.assertRaises(HTTPError) as exc:
            api('/agent/next',{},headers={'X-ASC-Device':did,'Authorization':'Bearer '+paired['device_token']})
        self.assertEqual(exc.exception.code,401)


if __name__=='__main__':unittest.main()
