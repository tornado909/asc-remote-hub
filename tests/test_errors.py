import http.cookiejar
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/"server"))
from hub import Hub,Failure,Handler,ThreadingHTTPServer
from traffic_trace import TrafficTrace

class ErrorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub=Hub(base_url="http://127.0.0.1:9876",data_dir=self.tmp.name,test_mode=True)
        self.user=self.hub.register_user("owner@example.test","OwnerPassword2026Strong")
        self.other=self.hub.register_user("other@example.test","OtherPassword2026Strong")
        self.did=self.hub.pairing_redeem(
            self.hub.pairing_create(self.user["id"]),"Own Windows","Windows",["machine_info"])["device_id"]
        self.foreign=self.hub.pairing_redeem(
            self.hub.pairing_create(self.other["id"]),"Other Linux","Linux",["machine_info"])["device_id"]

    def test_error_metadata_is_scoped_filtered_and_paged(self):
        uid=self.user["id"]
        self.hub.track(uid,self.did,"machine_info",True,
                       message="AccessDenied: Path is outside ASC_RC_ROOTS",request_id="trace-a")
        self.hub.track(uid,self.did,"read_file",True,
                       message="FileNotFoundError: missing",request_id="trace-b")
        self.hub.track(uid,self.did,"machine_info",False)
        self.hub.track(self.other["id"],self.foreign,"run_command",True,
                       message="Private user failure",request_id="other-request")
        answer=self.hub.errors(uid)
        self.assertEqual(answer["total"],2)
        self.assertEqual(len(answer["events"]),2)
        self.assertEqual({x["tool"] for x in answer["events"]},{"machine_info","read_file"})
        self.assertEqual(self.hub.errors(uid,device=self.did)["total"],2)
        self.assertEqual(self.hub.errors(uid,tool="read_file")["total"],1)
        page=self.hub.errors(uid,limit=1)
        self.assertEqual(page["total"],2)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(self.hub.errors(uid,limit=1,offset=1)["events"]),1)
        self.assertFalse(any("Private" in x["message"] for x in answer["events"]))
        self.assertEqual(self.hub.usage(uid)["errors"],2)
        with self.assertRaises(Failure) as error:
            self.hub.errors(uid,device=self.foreign)
        self.assertEqual(error.exception.status,404)
        for kwargs in ({"days":0},{"days":31},{"limit":101},{"offset":-1},{"tool":"../../"}):
            with self.assertRaises(Failure):self.hub.errors(uid,**kwargs)

    def test_agent_error_is_saved_by_forward_path(self):
        d=self.hub.row("SELECT * FROM devices WHERE id=?",(self.did,))
        self.hub.agent_hello(d,{"machine_info":{"name":"test"},"tools":["machine_info"]})
        returned={}
        thread=threading.Thread(target=lambda:returned.update(
            response=self.hub.forward(self.user["id"],"machine_info",
                                      {"device":self.did},request_id="test-forward-123")))
        thread.start()
        job=self.hub.agent_poll(d)["job"]
        self.assertIsNotNone(job)
        self.hub.agent_result(d,{"id":job["id"],"result":{
            "content":[{"type":"text","text":"AccessDenied: Protected path"}],"isError":True
        }})
        thread.join(timeout=7)
        self.assertFalse(thread.is_alive())
        self.assertTrue(returned["response"]["isError"])
        errors=self.hub.errors(self.user["id"])["events"]
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0]["request_id"],"test-forward-123")
        self.assertEqual(errors[0]["job_id"],job["id"])
        self.assertEqual(errors[0]["error_type"],"AccessDenied")
        self.assertEqual(errors[0]["device_name"],"Own Windows")
        self.assertEqual(errors[0]["message"],"AccessDenied: Protected path")

    def test_errors_survive_trace_disable_and_retention(self):
        self.hub.track(self.user["id"],self.did,"read_file",True,message="Error: persistent")
        self.assertFalse(self.hub.trace.enabled)
        self.assertEqual(self.hub.errors(self.user["id"])["total"],1)
        event=self.hub.errors(self.user["id"])["events"][0]
        self.assertFalse(self.hub.error_trace(self.user["id"],event["id"])["available"])
        self.hub.execute("UPDATE error_events SET created=? WHERE id=?",
                         (time.time()-35*86400,event["id"]))
        self.hub.cleanup_expired()
        self.assertEqual(self.hub.errors(self.user["id"])["total"],0)

    def test_private_http_error_list_and_trace_endpoints(self):
        folder=Path(self.tmp.name)
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
        sock.close()
        http=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        http.daemon_threads=True
        http.hub=self.hub
        th=threading.Thread(target=http.serve_forever,daemon=True)
        th.start()
        self.addCleanup(lambda:(http.shutdown(),http.server_close(),th.join(5)))
        base=f"http://127.0.0.1:{port}"
        sid1,_=self.hub.session_create(self.user["id"])
        sid2,_=self.hub.session_create(self.other["id"])

        event_id="err_0123456789abcdef0123456789abcdef"
        traceid="test-trace-012345"
        self.hub.execute(
            """INSERT INTO error_events
            (id,user_id,created,device_id,tool,error_type,message,request_id,job_id,source)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_id,self.user["id"],time.time(),self.did,"read_file",
             "AccessDenied","AccessDenied: private metadata",traceid,None,"agent"))
        self.hub.trace=TrafficTrace(folder,enabled=True)
        for direction in ("chatgpt_to_hub","hub_to_chatgpt"):
            self.hub.trace.record(direction=direction,route="/mcp",user_id=self.user["id"],
               device_id=self.did,request_id=traceid,payload={"marker":"Private original"})
        def get(path,sid=None):
            req=Request(base+path,headers={"Cookie":"asc_sid="+sid} if sid else {})
            return urlopen(req,timeout=6)
        with get("/api/errors",sid1) as response:
            result=json.load(response)
        self.assertEqual(result["total"],1)
        self.assertEqual(result["events"][0]["id"],event_id)
        with get("/api/errors/"+event_id+"/trace",sid1) as response:
            trace=json.load(response)
        self.assertTrue(trace["available"])
        self.assertEqual(len(trace["records"]),2)
        self.assertTrue(all(x["payload"]["marker"]=="Private original" for x in trace["records"]))
        for path in ("/api/errors","/api/errors/"+event_id+"/trace"):
            with self.assertRaises(HTTPError) as err:get(path,None)
            self.assertEqual(err.exception.code,401)
        # Other user can see only their empty list, never owner's errors.
        with get("/api/errors",sid2) as response:
            self.assertEqual(json.load(response)["total"],0)
        with self.assertRaises(HTTPError) as err:get("/api/errors/"+event_id+"/trace",sid2)
        self.assertEqual(err.exception.code,404)
        with self.assertRaises(HTTPError) as err:get("/api/errors/"+event_id+"/trace")
        self.assertEqual(err.exception.code,401)
        with self.assertRaises(HTTPError) as err:get("/api/errors?days=abc",sid1)
        self.assertEqual(err.exception.code,400)
        with self.assertRaises(HTTPError) as err:get("/api/errors?device="+self.foreign,sid1)
        self.assertEqual(err.exception.code,404)

    def test_error_source_cannot_expose_another_tenant_by_device(self):
        other_event="err_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.hub.execute(
            """INSERT INTO error_events
            (id,user_id,created,device_id,tool,error_type,message,request_id,source)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (other_event,self.other["id"],time.time(),self.foreign,
             "machine_info","OtherError","sensitive-other-account","request-other","agent"))
        self.hub.trace=TrafficTrace(Path(self.tmp.name),enabled=True)
        self.assertFalse(self.hub.trace.path_for(self.user["id"],self.did).exists())
        with self.assertRaises(Failure) as err:
            self.hub.error_trace(self.user["id"],other_event)
        self.assertEqual(err.exception.status,404)
        self.assertEqual(self.hub.errors(self.user["id"])["total"],0)
