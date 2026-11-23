"""Pinned Windows C ABI integration without loading a real DLL on CI."""
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
from asc_desktop_ffi import (
    ABI_MAJOR,ACTION_BYTES,ACTION_KIND,DLL_SHA256,DLL_ARCHIVE_SHA256,
    DesktopFFI,available,RC_CODES
)
from asc_desktop_native import NativeDesktop

class FFIIntegrationTests(unittest.TestCase):
    def test_pinned_native_release_and_abi_guards(self):
        self.assertEqual(ABI_MAJOR,7)
        self.assertEqual(ACTION_BYTES,96)
        self.assertRegex(DLL_SHA256,r"^[a-f0-9]{64}$")
        self.assertRegex(DLL_ARCHIVE_SHA256,r"^[a-f0-9]{64}$")
        self.assertEqual(ACTION_KIND["click"],0)
        self.assertEqual(ACTION_KIND["set-value"],4)
        self.assertEqual(ACTION_KIND["type"],17)
        self.assertFalse(available())

    def test_installer_contains_official_pinned_ffi_release(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location(
            "ffi_installer",ROOT/"install/setup_native_desktop.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.DLL_ARCHIVE_SHA256,DLL_ARCHIVE_SHA256)
        self.assertIn("vercel-labs/agent-desktop",module.FFI_SOURCE)
        self.assertIn("v1.3.1",module.FFI_SOURCE)
        self.assertLess(module.MAX_FFI_ARCHIVE,5_000_000)

    def test_native_status_snapshot_and_action_ffi_routing_opt_in(self):
        with tempfile.TemporaryDirectory() as temp,\
             mock.patch("asc_desktop_native.installed",return_value=True),\
             mock.patch("asc_desktop_native.native_executable",return_value=Path(temp)/"app.exe"):
            native=NativeDesktop(Path(temp))
            backend=mock.Mock()
            backend.status.return_value={"ok":True}
            backend.snapshot.return_value={"ok":True,"data":{"ref_count":3}}
            backend.act.return_value={"ok":True}
            with mock.patch.object(native,"_ffi",return_value=backend) as getffi,\
                 mock.patch.object(native,"_run",side_effect=AssertionError("No subprocess CLI")):
                native.status({"engine":"ffi","session_id":"test"})
                native.snapshot({"app":"Editor","engine":"ffi","max_depth":3})
                native.act({"ref":"@sabc:e1","action":"set-value","value":"Secret","engine":"ffi"})
            self.assertEqual(getffi.call_count,3)
            backend.status.assert_called_once_with("test")
            backend.snapshot.assert_called_once_with("Editor","window",3,True,True,None)
            backend.act.assert_called_once_with("@sabc:e1","set-value","Secret",7000,None)

    def test_ffi_rejects_incompatible_window_scope_and_wait(self):
        with tempfile.TemporaryDirectory() as temp,\
             mock.patch("asc_desktop_native.installed",return_value=True),\
             mock.patch("asc_desktop_native.native_executable",return_value=Path(temp)/"app.exe"):
            native=NativeDesktop(Path(temp))
            with mock.patch.object(native,"_ffi",side_effect=AssertionError("Must not load FFI")):
                with self.assertRaises(ValueError):
                    native.snapshot({"engine":"ffi","window_id":"w-12"})
                with self.assertRaises(ValueError):
                    native.act({"ref":"@sabc:e1","action":"click","engine":"ffi",
                                "wait_for":"button:Done"})
                with self.assertRaises(ValueError):
                    native.status({"engine":"unknown"})

    def test_ffi_action_redacts_secret_from_response(self):
        obj=DesktopFFI.__new__(DesktopFFI)
        obj.lock=threading.RLock()
        data={"ok":True,"data":{"disposition":{"delivery":"delivered_verified","retry":"unsafe"},
                                 "post_state":{"value":"TOP_SECRET","text":"TOP_SECRET"},
                                 "action":"set-value"}}
        class FakeLib:
            def __init__(self):
                self.buffer=ctypes.create_string_buffer(json.dumps(data).encode())
                self.called=False
                self.kind=None
            def ad_execute_by_ref_timeout(self,adapter,reference,action,policy,timeout,out):
                self.called=True
                self.kind=ctypes.c_int32.from_address(action.value).value
                self.assert_ref=reference
                ctypes.cast(out,ctypes.POINTER(ctypes.c_void_p))[0]=ctypes.addressof(self.buffer)
                return 0
            def ad_free_string(self,ptr):
                pass
        obj.lib=FakeLib()
        obj._adapter=lambda sid:123
        result=obj.act("@sabc:e3","set-value","TOP_SECRET",5000)
        self.assertEqual(obj.lib.kind,ACTION_KIND["set-value"])
        self.assertTrue(obj.lib.called)
        self.assertEqual(result["data"]["post_state"]["value"],{"redacted":True})
        self.assertNotIn("TOP_SECRET",json.dumps(result))

    def test_ffi_failure_does_not_expose_private_error_details(self):
        from asc_desktop_native import NativeDesktopError
        sample={"ok":False,"error":{"code":"STALE_REF","message":"Private editor tab",
                "disposition":{"delivery":"not_delivered","retry":"safe"}}}
        contents=json.dumps(sample).encode()
        buffer=ctypes.create_string_buffer(contents)
        class FakeLib:
            def ad_free_string(self,ptr):
                pass
        pointer=ctypes.c_void_p(ctypes.addressof(buffer))
        with self.assertRaises(NativeDesktopError) as exc:
            DesktopFFI._decode(-6,pointer,FakeLib(),True)
        self.assertEqual(exc.exception.code,"STALE_REF")
        self.assertNotIn("Private",str(exc.exception))
        self.assertEqual(exc.exception.disposition["retry"],"safe")


if __name__=="__main__":
    unittest.main()
