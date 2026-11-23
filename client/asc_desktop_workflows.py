"""Bounded semantic workflows over the pinned agent-desktop native engine."""
from __future__ import annotations
import json
import re
import time
from typing import Any, Callable

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
WINDOW_ID = re.compile(r"w-[0-9]+\Z")
UI_REF = re.compile(r"@s[a-z0-9]+:e[1-9][0-9]{0,8}\Z")
WINDOW_SURFACES = frozenset(("window","focused","menu","sheet","alert","taskbar",
                             "system-tray","system-tray-overflow","start-menu","action-center"))
WAIT_EVENTS = frozenset(("window-opened","window-closed","app-launched","app-terminated",
                         "focus-changed","surface-appeared","surface-dismissed"))
READ_OPS = frozenset(("status","windows","apps","surfaces","snapshot","find","get","is","wait","verify"))
BATCH_OPS = READ_OPS | {"act","window"}
WINDOW_ACTIONS = frozenset(("focus","resize","move","minimize","maximize","restore"))
ACTION_WAIT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,40}:[^:\r\n\x00]{1,140}\Z")

def string(v:Any,key:str,max_length:int=128,required:bool=False)->str:
    if v is None:
        if required:raise ValueError(f"{key} is required")
        return ""
    if not isinstance(v,str) or "\x00" in v or len(v)>max_length or (required and not v):
        raise ValueError(f"Invalid {key}")
    return v

def number(v:Any,key:str,default:int|None,minimum:int,maximum:int)->int|None:
    if v is None:return default
    if isinstance(v,bool) or not isinstance(v,int) or not minimum<=v<=maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return v

def boolean(v:Any,key:str,default:bool=False)->bool:
    if v is None:return default
    if not isinstance(v,bool):raise ValueError(f"{key} must be boolean")
    return v

def session_scope(a:dict)->str|None:
    sid=a.get("session_id")
    if sid is None:return None
    if not isinstance(sid,str) or not SESSION_ID.fullmatch(sid):
        raise ValueError("Invalid session_id")
    return sid

def ref(value:Any)->str:
    s=string(value,"ref",80,True)
    if not UI_REF.fullmatch(s):raise ValueError("Expected qualified @snapshot:eN ref")
    return s

def native_scope(a:dict,allow_root:bool=False,require_target:bool=False)->list[str]:
    wid=string(a.get("window_id"),"window_id",32)
    app=string(a.get("app"),"app",128)
    root=string(a.get("root"),"root",80) if allow_root else ""
    if wid and not WINDOW_ID.fullmatch(wid):raise ValueError("Invalid window_id")
    if root:ref(root)
    if sum(bool(x) for x in (wid,app,root))>1:raise ValueError("Choose only one app, window_id or root")
    if require_target and not any((wid,app,root)):raise ValueError("Explicit app or window_id required")
    return ["--root",root] if root else ["--window-id",wid] if wid else ["--app",app] if app else []

class WorkflowMixin:
    def surfaces(self,a:dict)->dict:
        app=string(a.get("app"),"app",128,True)
        return self._run("list-surfaces",["--app",app],session_id=session_scope(a))

    def apps(self,a:dict)->dict:
        op=string(a.get("operation"),"operation",16,True)
        app=string(a.get("app"),"app",128)
        sid=session_scope(a)
        if op=="list":
            t=number(a.get("timeout_ms"),"timeout_ms",8000,500,20000)
            return self._run("list-apps",(["--app",app] if app else [])+["--timeout-ms",str(t)],timeout_ms=t,session_id=sid)
        if op=="launch":
            if not app:raise ValueError("launch requires exact app name from list-apps")
            t=number(a.get("timeout_ms"),"timeout_ms",20000,1000,30000)
            return self._run("launch",[app,"--timeout",str(t)],timeout_ms=t,session_id=sid)
        if op=="close":
            if not app:raise ValueError("close requires exact app name")
            return self._run("close-app",[app],timeout_ms=15000,session_id=sid)
        raise ValueError("Unknown app operation")

    def window(self,a:dict)->dict:
        op=string(a.get("operation"),"operation",16,True)
        if op not in WINDOW_ACTIONS:raise ValueError("Unknown window operation")
        scope=native_scope(a,require_target=True)
        sid=session_scope(a)
        if op in ("focus","minimize","maximize","restore"):
            return self._run("focus-window" if op=="focus" else op,scope,timeout_ms=10000,session_id=sid)
        if op=="resize":
            w=number(a.get("width"),"width",None,100,7680)
            h=number(a.get("height"),"height",None,100,4320)
            if w is None or h is None:raise ValueError("resize requires width and height")
            return self._run("resize-window",scope+["--width",str(w),"--height",str(h)],timeout_ms=12000,session_id=sid)
        x=number(a.get("x"),"x",None,-20000,20000)
        y=number(a.get("y"),"y",None,-20000,20000)
        if x is None or y is None:raise ValueError("move requires x and y")
        return self._run("move-window",scope+[f"--x={x}",f"--y={y}"],timeout_ms=12000,session_id=sid)

    def session(self,a:dict)->dict:
        op=string(a.get("operation"),"operation",12,True)
        if op=="start":
            name=string(a.get("name"),"name",64)
            return self._run("session",["start","--no-trace"]+(["--name",name] if name else []),timeout_ms=8000)
        if op=="list":return self._run("session",["list"],timeout_ms=5000)
        if op=="end":
            sid=session_scope(a)
            if not sid:raise ValueError("end requires session_id")
            return self._run("session",["end",sid],timeout_ms=5000)
        if op=="gc":
            age=number(a.get("older_than_seconds"),"older_than_seconds",86400,3600,2592000)
            return self._run("session",["gc","--ended","--older-than",str(age)],timeout_ms=10000)
        raise ValueError("Unknown session operation")

    def wait(self,a:dict)->dict:
        mode=string(a.get("mode"),"mode",20,True)
        if mode not in ("element","window","text","menu","menu-closed","event","notification"):
            raise ValueError("Unknown wait mode")
        t=number(a.get("timeout_ms"),"timeout_ms",10000,200,30000)
        opts=["--timeout",str(t)]
        app=string(a.get("app"),"app",128)
        if mode=="element":
            pred=string(a.get("predicate"),"predicate",20) or "exists"
            if pred not in ("exists","enabled","visible","actionable"):raise ValueError("Invalid wait predicate")
            opts+=["--element",ref(a.get("ref")),"--predicate",pred]
            if pred=="actionable":
                action=string(a.get("action"),"action",16) or "click"
                if action not in ("click","type","set-value","clear"):raise ValueError("Invalid wait action")
                opts+=["--action",action]
        elif mode=="window":opts+=["--window",string(a.get("window"),"window",180,True)]
        elif mode=="text":opts+=["--text",string(a.get("text"),"text",150,True)]
        elif mode in ("menu","menu-closed"):opts+=["--"+mode]
        elif mode=="event":
            event=string(a.get("event"),"event",40,True)
            if event not in WAIT_EVENTS:raise ValueError("Invalid wait event")
            opts+=["--event",event]
            wid=string(a.get("window_id"),"window_id",32)
            if wid:
                if not WINDOW_ID.fullmatch(wid):raise ValueError("Invalid window_id")
                opts+=["--window-id",wid]
        else:
            opts+=["--notification"]
            if a.get("text"):opts+=["--text",string(a["text"],"text",150,True)]
        if app:
            if mode=="element":raise ValueError("Element ref already scopes app")
            opts+=["--app",app]
        return self._run("wait",opts,timeout_ms=t,session_id=session_scope(a))

    def verify(self,a:dict)->dict:
        """Private value predicate: compare in RAM, never put secret into argv."""
        target=ref(a.get("ref"))
        expected=string(a.get("value"),"value",4000,True)
        t=number(a.get("timeout_ms"),"timeout_ms",10000,500,25000)
        sid=session_scope(a)
        start=time.monotonic()
        attempts=0
        while True:
            attempts+=1
            response=self._run("get",[target,"--property","value"],timeout_ms=min(5000,t),session_id=sid)
            data=response.get("data")
            observed=data.get("value") if isinstance(data,dict) else data if isinstance(data,str) else None
            if observed==expected:
                return {"ok":True,"matched":True,"attempts":attempts,
                        "elapsed_ms":round((time.monotonic()-start)*1000),"observed_value_redacted":True}
            remaining=t/1000-(time.monotonic()-start)
            if remaining<=0:
                return {"ok":False,"matched":False,"code":"TIMEOUT","attempts":attempts,
                        "elapsed_ms":round((time.monotonic()-start)*1000),"observed_value_redacted":True}
            time.sleep(min(0.25,remaining))

    @staticmethod
    def _find_ref(result:dict)->str:
        data=result.get("data")
        if not isinstance(data,dict):raise ValueError("Find result missing")
        match=data.get("match")
        if isinstance(match,dict) and match.get("ref_id"):return ref(match["ref_id"])
        matches=data.get("matches")
        if isinstance(matches,list) and len(matches)==1 and isinstance(matches[0],dict):
            return ref(matches[0].get("ref_id"))
        raise ValueError("Find result ref missing or ambiguous")

    def batch(self,a:dict,mutate_preflight:Callable[[],None])->dict:
        """Sequential, non-atomic operations; secret values use native stdin."""
        entries=a.get("steps")
        if not isinstance(entries,list) or not 1<=len(entries)<=12:
            raise ValueError("steps must contain 1..12 operations")
        sid=session_scope(a)
        budget=number(a.get("timeout_ms"),"timeout_ms",30000,1000,60000)
        stop=boolean(a.get("stop_on_error"),"stop_on_error",True)
        permitted={
            "status":{"engine"},"windows":{"app","timeout_ms"},"apps":{"operation","app","timeout_ms"},
            "surfaces":{"app"},"snapshot":{"engine","app","window_id","root","surface","interactive_only","compact","skeleton","max_depth","timeout_ms"},
            "find":{"app","window_id","root","surface","role","name","exact","first","timeout_ms"},
            "get":{"ref","property","ref_from"},"is":{"ref","property","ref_from"},
            "verify":{"ref","value","timeout_ms","ref_from"},
            "wait":{"mode","ref","ref_from","predicate","action","window","event","window_id","app","text","timeout_ms"},
            "act":{"engine","ref","ref_from","action","value","timeout_ms","wait_for","wait_for_gone","wait_timeout_ms"},
            "window":{"operation","app","window_id","width","height","x","y"}}
        for i,step in enumerate(entries):
            if not isinstance(step,dict) or set(step)!={"op","args"}:
                raise ValueError(f"Step {i} needs op and args")
            op=step["op"];args=step["args"]
            if op not in BATCH_OPS or not isinstance(args,dict) or set(args)-permitted[op]:
                raise ValueError(f"Unsupported step {i}")
            if "ref_from" in args:
                parent=args["ref_from"]
                if (isinstance(parent,bool) or not isinstance(parent,int) or
                    parent<0 or parent>=i or op not in ("act","get","is","wait","verify") or
                    "ref" in args or entries[parent]["op"]!="find"):
                    raise ValueError("Invalid ref_from dependency")
        start=time.monotonic()
        previous_deadline=getattr(self,"_batch_deadline",None)
        self._batch_deadline=start+budget/1000
        try:
            results=[]
            mapping={"status":self.status,"windows":self.windows,"apps":self.apps,
                     "surfaces":self.surfaces,"snapshot":self.snapshot,"find":self.find,
                     "get":self.get,"is":self.is_state,"wait":self.wait,"verify":self.verify,
                     "act":self.act,"window":self.window}
            for i,step in enumerate(entries):
                op=step["op"];args=dict(step["args"])
                elapsed=round((time.monotonic()-start)*1000)
                if elapsed>=budget:
                    results.append({"index":i,"op":op,"ok":False,"code":"TIMEOUT",
                                    "disposition":{"delivery":"unknown","retry":"unknown"}})
                    break
                args["session_id"]=sid
                try:
                    if "ref_from" in args:
                        j=args.pop("ref_from")
                        args["ref"]=self._find_ref(results[j]["result"])
                    if "timeout_ms" in args:
                        args["timeout_ms"]=min(number(args["timeout_ms"],"timeout_ms",7000,200,60000),max(200,budget-elapsed))
                    if op in ("act","window") or (op=="apps" and args.get("operation")!="list"):
                        mutate_preflight()
                    value=mapping[op](args)
                    if len(json.dumps(value,ensure_ascii=False,default=str).encode("utf-8"))>12000:
                        data=value.get("data",{}) if isinstance(value,dict) else {}
                        safe_data={key:data[key] for key in (
                            "snapshot_id","ref_count","complete","total","match","matches",
                            "elapsed_ms","disposition") if isinstance(data,dict) and key in data}
                        if isinstance(safe_data.get("matches"),list):
                            safe_data["matches"]=safe_data["matches"][:1]
                        value={"ok":True,"truncated_for_batch":True,"data":safe_data,
                               "hint":"Use a dedicated snapshot/find call to read the full accessibility tree"}
                    results.append({"index":i,"op":op,"ok":True,"result":value})
                except Exception as exc:
                    code=getattr(exc,"code",None) or ("INVALID_ARGS" if isinstance(exc,ValueError) else "ENGINE_FAILED")
                    is_mutation=op in ("act","window") or (op=="apps" and args.get("operation")!="list")
                    known_local_refusal=isinstance(exc,ValueError)
                    safe=known_local_refusal or not is_mutation
                    disposition=getattr(exc,"disposition",None) or {
                        "delivery":"not_delivered" if safe else "unknown",
                        "retry":"safe" if safe else "unknown"}
                    results.append({"index":i,"op":op,"ok":False,"code":code,"disposition":disposition})
                    if stop or disposition.get("retry")!="safe":break
            return {"ok":len(results)==len(entries) and all(x["ok"] for x in results),
                    "atomic":False,"order":"sequential","steps_total":len(entries),
                    "steps_completed":len(results),"results":results,
                    "elapsed_ms":round((time.monotonic()-start)*1000)}
        finally:
            self._batch_deadline=previous_deadline
