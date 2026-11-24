import json, pathlib, sys, tempfile, threading, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from hub import Hub, Failure
class AndroidAgentTests(unittest.TestCase):
 def test_android_pair_and_relay(self):
  with tempfile.TemporaryDirectory() as tmp:
   h=Hub(base_url='http://127.0.0.1:9876',data_dir=tmp,test_mode=True)
   u=h.register_user('android@example.org','AndroidTest-Password-1234')
   other=h.register_user('other@example.org','OtherTest-Password-1234')
   names=['machine_info','list_directory','stat_path','android_apps','android_launch','android_ui_tree','android_ui_action','android_gesture','android_file_read','android_file_write']
   code=h.pairing_create(u['id'])
   d=h.pairing_redeem(code,'Android test','Android 17',names)
   with self.assertRaises(Failure): h.pairing_redeem(code,'Again','Android 17',names)
   identity=h.agent_identity(d['device_id'],d['device_token'])
   h.agent_hello(identity,{'machine_info':{'platform':'Android 17'},'tools':names,'version':'0.1.0'})
   self.assertEqual(set(h.list_devices(u['id'])[0]['tools']),set(names))
   self.assertTrue(h.forward(other['id'],'android_apps',{'device':d['device_id']})['isError'])
   result=[]
   def forward(): result.append(h.forward(u['id'],'android_file_read',{'device':d['device_id'],'path':'Download/example.bin','offset':0,'length':4}))
   t=threading.Thread(target=forward);t.start()
   job=h.agent_poll(identity)['job']
   self.assertEqual(job['tool'],'android_file_read')
   self.assertEqual(job['arguments']['path'],'Download/example.bin')
   h.agent_result(identity,{'id':job['id'],'result':{'content':[{'type':'text','text':'{"data_base64":"AAECAw==","eof":true}'}],'isError':False}})
   t.join(5);self.assertFalse(t.is_alive());self.assertFalse(result[0]['isError'])
   gesture={'device':d['device_id'],'snapshot':'test-snapshot','strokes':[{'points':[{'x':100,'y':900},{'x':100,'y':200}],'duration_ms':400}]}
   self.assertTrue(h.forward(other['id'],'android_gesture',gesture)['isError'])
   result.clear()
   t=threading.Thread(target=lambda:result.append(h.forward(u['id'],'android_gesture',gesture)));t.start()
   job=h.agent_poll(identity)['job']
   self.assertEqual(job['tool'],'android_gesture')
   self.assertEqual(job['arguments'],{k:v for k,v in gesture.items() if k!='device'})
   h.agent_result(identity,{'id':job['id'],'result':{'content':[{'type':'text','text':'{"ok":true,"status":"completed"}'}],'isError':False}})
   t.join(5);self.assertFalse(t.is_alive());self.assertFalse(result[0]['isError'])
   h.device_revoke(identity)
   with self.assertRaises(Failure): h.agent_identity(d['device_id'],d['device_token'])
 def test_release_manifest(self):
  import xml.etree.ElementTree as ET
  tree=ET.parse(ROOT/'android/AndroidManifest.xml')
  ns='{http://schemas.android.com/apk/res/android}'
  self.assertEqual(tree.find('uses-sdk').get(ns+'minSdkVersion'),'30')
  self.assertEqual(tree.find('application').get(ns+'allowBackup'),'false')
  services=tree.findall('application/service')
  self.assertEqual(services[0].get(ns+'exported'),'false')
  self.assertEqual(services[1].get(ns+'permission'),'android.permission.BIND_ACCESSIBILITY_SERVICE')

 def test_gesture_manifest(self):
  import xml.etree.ElementTree as ET
  ns='{http://schemas.android.com/apk/res/android}'
  self.assertEqual(ET.parse(ROOT/'android/res/xml/accessibility.xml').getroot().get(ns+'canPerformGestures'),'true')
  tools=json.loads((ROOT/'server/tools_manifest.json').read_text())
  schema=next(x['inputSchema'] for x in tools if x['name']=='android_gesture')
  self.assertEqual(set(schema['required']),{'device','snapshot','strokes'})
  self.assertEqual(schema['properties']['strokes']['maxItems'],5)
  self.assertEqual(schema['properties']['strokes']['items']['properties']['points']['maxItems'],64)
