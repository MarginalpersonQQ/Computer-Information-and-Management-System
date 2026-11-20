import sys, tempfile, threading, unittest, json, urllib.request, urllib.error, http.cookiejar
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import server

class SystemTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();server.DATA=Path(cls.tmp.name);server.initialize()
  with server.db() as c:
   for name,role in [('boss','admin'),('worker','tech'),('reader','viewer')]:
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(name,name,name,server.password_hash('testing-pass-123'),role))
  cls.s=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=cls.s.serve_forever,daemon=True).start();cls.base='http://127.0.0.1:'+str(cls.s.server_port)
 @classmethod
 def tearDownClass(cls): cls.s.shutdown();cls.s.server_close();cls.tmp.cleanup()
 def client(self,name):
  op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
  self.call(op,'login',{'username':name,'password':'testing-pass-123'})
  return op,self.call(op,'state')[1]['me']['csrf']
 def call(self,op,path,data=None,csrf=''):
  req=urllib.request.Request(self.base+'/api/'+path,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
  try:
   with op.open(req) as r:
    body=r.read();return r.status,json.loads(body) if r.headers['Content-Type'].startswith('application/json') else body
  except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
 def delete_customer(self,op,token,id):
  status,info=self.call(op,'customer/delete-preview',{'id':id},token)
  if status!=200:return status,info
  return self.call(op,'customer/delete',{'id':id,'confirm':'DELETE','confirm_name':info['name'],'snapshot':info['snapshot']},token)
 def test_customer_cascade_delete(self):
  import io,base64
  from PIL import Image
  boss,csrf=self.client('boss');tech,t=self.client('worker');viewer,v=self.client('reader')
  _,customer=self.call(boss,'customer',{'name':'對接人','customer_type':'company','company_name':'整批刪除測試公司'},csrf)
  _,other=self.call(boss,'customer',{'name':'保留的其他客戶'},csrf)
  _,outside=self.call(boss,'device',{'name':'不可誤刪的電腦','customer_id':other['id']},csrf)
  devices=[]
  for name in ['使用中電腦','已封存電腦']:
   _,device=self.call(boss,'device',{'name':name,'customer_id':customer['id'],'credentials':{'windows_password':'dummy-only','accounts':[]}},csrf);devices.append(device['id'])
  self.call(boss,'device/archive',{'id':devices[1],'archived':True},csrf)
  _,repair=self.call(boss,'repair',{'device_id':devices[0],'date':'2026-10-19','status':'維修中','problem':'不可漏刪','items':[],'amount':10,'paid':False,'notes':''},csrf)
  raw=io.BytesIO();Image.new('RGB',(32,32),'green').save(raw,format='PNG')
  _,photo=self.call(boss,'photo/upload',{'device_id':devices[1],'image':base64.b64encode(raw.getvalue()).decode()},csrf)
  for op,token in [(tech,t),(viewer,v)]:
   self.assertEqual(self.call(op,'customer/delete-preview',{'id':customer['id']},token)[0],403)
   self.assertEqual(self.call(op,'customer/delete',{'id':customer['id'],'confirm':'DELETE'},token)[0],403)
  _,info=self.call(boss,'customer/delete-preview',{'id':customer['id']},csrf)
  self.assertEqual(info['name'],'整批刪除測試公司');self.assertEqual(info['counts'],{'devices':2,'repairs':1,'photos':1,'credentials':2})
  request={'id':customer['id'],'confirm':'DELETE','confirm_name':info['name'],'snapshot':info['snapshot']}
  self.assertEqual(self.call(boss,'customer/delete',{**request,'confirm_name':'錯誤名稱'},csrf)[0],400)
  self.assertEqual(self.call(boss,'customer/delete',{**request,'snapshot':''},csrf)[0],409)
  with server.db() as con:con.execute('UPDATE repairs SET notes=? WHERE id=?',('確認期間有人修改了紀錄',repair['id']))
  self.assertEqual(self.call(boss,'customer/delete',request,csrf)[0],409)
  self.assertEqual(self.delete_customer(boss,csrf,customer['id'])[0],200)
  state=self.call(boss,'state')[1];self.assertNotIn(customer['id'],[r['id'] for r in state['customers']]);self.assertIn(outside['id'],[r['id'] for r in state['devices']])
  for id in devices:
   self.assertNotIn(id,[r['id'] for r in state['devices']]);self.assertEqual(self.call(boss,'qr?id='+id)[0],404)
   with server.db() as con:self.assertEqual(con.execute('SELECT count(*) FROM device_secrets WHERE device_id=?',(id,)).fetchone()[0],0)
  self.assertNotIn(repair['id'],[r['id'] for r in state['repairs']]);self.assertEqual(self.call(boss,'photo?id='+photo['id'])[0],404)
  self.assertEqual(self.delete_customer(boss,csrf,other['id'])[0],200)
 def test_category_management(self):
  boss,csrf=self.client('boss');tech,t=self.client('worker');viewer,v=self.client('reader')
  original=self.call(boss,'state')[1]['categories']
  _,c=self.call(boss,'customer',{'name':'分類歷史測試'},csrf)
  _,d=self.call(boss,'device',{'customer_id':c['id'],'name':'分類歷史測試電腦'},csrf)
  item='軟體安裝 → Office'
  _,r=self.call(boss,'repair',{'device_id':d['id'],'date':'2026-10-19','status':'已結案','problem':'歷史項目保留測試','items':[item],'notes':'','amount':0,'paid':True},csrf)
  for op,token in [(tech,t),(viewer,v)]:self.assertEqual(self.call(op,'categories',{'categories':{},'expected':original},token)[0],403)
  import copy
  current=copy.deepcopy(original)
  renamed={('應用軟體' if k=='軟體安裝' else k):values for k,values in current.items()}
  self.assertEqual(self.call(boss,'categories',{'categories':renamed,'expected':current},csrf)[0],200)
  self.assertEqual(self.call(boss,'categories',{'categories':{},'expected':current},csrf)[0],409)
  current=copy.deepcopy(renamed);renamed=copy.deepcopy(current);renamed['應用軟體']=['Microsoft 365' if i=='Office' else i for i in renamed['應用軟體']]
  self.assertEqual(self.call(boss,'categories',{'categories':renamed,'expected':current},csrf)[0],200)
  current=copy.deepcopy(renamed);renamed=copy.deepcopy(current);renamed['應用軟體'].remove('Microsoft 365')
  self.assertEqual(self.call(boss,'categories',{'categories':renamed,'expected':current},csrf)[0],200)
  current=copy.deepcopy(renamed);renamed=copy.deepcopy(current);del renamed['應用軟體']
  self.assertEqual(self.call(boss,'categories',{'categories':renamed,'expected':current},csrf)[0],200)
  history=next(a for a in self.call(boss,'state')[1]['repairs'] if a['id']==r['id']);self.assertEqual(history['items'],[item])
  self.assertEqual(self.call(boss,'categories',{'categories':{'大項':['重複','重複']}},csrf)[0],400)
  self.assertEqual(self.call(boss,'categories',{'categories':original,'expected':renamed},csrf)[0],200)
  with server.db() as con:con.execute('DELETE FROM repairs WHERE id=?',(r['id'],))
  self.assertEqual(self.call(boss,'device/delete',{'id':d['id'],'confirm':'DELETE'},csrf)[0],200)
  self.assertEqual(self.delete_customer(boss,csrf,c['id'])[0],200)
 def test_photos(self):
  import io,base64,sqlite3,subprocess,os
  from PIL import Image
  boss,csrf=self.client('boss');tech,t=self.client('worker');viewer,v=self.client('reader')
  _,c=self.call(boss,'customer',{'name':'照片測試客戶'},csrf)
  _,d=self.call(boss,'device',{'customer_id':c['id'],'name':'照片測試電腦'},csrf)
  image=Image.new('RGB',(2400,1200),(30,100,70));exif=Image.Exif();exif[270]='private-exif-note';raw=io.BytesIO();image.save(raw,format='JPEG',exif=exif)
  payload={'device_id':d['id'],'caption':'機殼外觀','image':base64.b64encode(raw.getvalue()).decode()}
  self.assertEqual(self.call(urllib.request.build_opener(),'photo/upload',payload)[0],401)
  self.assertEqual(self.call(viewer,'photo/upload',payload,v)[0],403)
  self.assertEqual(self.call(boss,'photo/upload',{**payload,'image':'bad-image'},csrf)[0],400)
  self.assertEqual(self.call(boss,'photo/upload',{**payload,'image':base64.b64encode(b'<svg></svg>').decode()},csrf)[0],400)
  code,p1=self.call(tech,'photo/upload',payload,t);self.assertEqual(code,200)
  self.assertEqual(self.call(urllib.request.build_opener(),'photo?id='+p1['id'])[0],401)
  code,content=self.call(viewer,'photo?id='+p1['id']);self.assertEqual(code,200)
  with Image.open(io.BytesIO(content)) as normalized:self.assertLessEqual(max(normalized.size),1600);self.assertEqual(len(normalized.getexif()),0)
  with Image.open(io.BytesIO(self.call(boss,'photo?id='+p1['id']+'&thumb=1')[1])) as thumb:self.assertLessEqual(max(thumb.size),320)
  self.assertNotIn(b'private-exif-note',content)
  metadata=next(p for p in self.call(boss,'state')[1]['photos'] if p['id']==p1['id']);self.assertEqual(metadata['is_cover'],1);self.assertNotIn('image',metadata)
  code,p2=self.call(boss,'photo/upload',payload,csrf);self.assertEqual(code,200)
  self.assertEqual(self.call(viewer,'photo/cover',{'id':p2['id']},v)[0],403)
  self.assertEqual(self.call(boss,'photo/cover',{'id':p2['id']},csrf)[0],200)
  covers=[p for p in self.call(boss,'state')[1]['photos'] if p['device_id']==d['id'] and p['is_cover']];self.assertEqual([p['id'] for p in covers],[p2['id']])
  self.assertEqual(self.call(viewer,'photo/delete',{'id':p2['id'],'confirm':'DELETE'},v)[0],403)
  self.assertEqual(self.call(boss,'photo/delete',{'id':p2['id']},csrf)[0],400)
  self.assertEqual(self.call(boss,'photo/delete',{'id':p2['id'],'confirm':'DELETE'},csrf)[0],200)
  self.assertEqual(self.call(boss,'photo?id='+p2['id'])[0],404)
  self.assertEqual(next(p for p in self.call(boss,'state')[1]['photos'] if p['id']==p1['id'])['is_cover'],1)
  for i in range(9):self.assertEqual(self.call(boss,'photo/upload',payload,csrf)[0],200)
  self.assertEqual(self.call(boss,'photo/upload',payload,csrf)[0],400)
  self.assertEqual(self.call(boss,'device/archive',{'id':d['id'],'archived':True},csrf)[0],200)
  self.assertEqual(self.call(boss,'photo?id='+p1['id'])[0],200)
  with tempfile.TemporaryDirectory() as directory:
   backup=Path(directory)/'photos.sqlite3';env={**os.environ,'DATA_DIR':str(server.DATA),'PYTHONPATH':os.pathsep.join(filter(None,[str(ROOT),os.environ.get('PYTHONPATH','')]))}
   result=subprocess.run([sys.executable,str(ROOT/'server.py'),'--backup',str(backup)],env=env,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)
   con=sqlite3.connect(backup);self.assertEqual(con.execute('SELECT image FROM device_photos WHERE id=?',(p1['id'],)).fetchone()[0],content);con.close()
  self.assertEqual(self.call(boss,'device/delete',{'id':d['id'],'confirm':'DELETE'},csrf)[0],200)
  self.assertEqual(self.call(boss,'photo?id='+p1['id'])[0],404)
  with server.db() as con:self.assertEqual(con.execute('SELECT count(*) FROM device_photos WHERE device_id=?',(d['id'],)).fetchone()[0],0)
  self.assertEqual(self.delete_customer(boss,csrf,c['id'])[0],200)
 def test_workflow(self):
  anon=urllib.request.build_opener();self.assertEqual(self.call(anon,'state')[0],401);self.assertEqual(self.call(anon,'qr?id=x')[0],401)
  boss,csrf=self.client('boss');self.assertEqual(self.call(boss,'customer',{'name':'小明'})[0],403)
  status,c=self.call(boss,'customer',{'name':'小明','phone':'0912345678','notes':'<script>not html</script>'},csrf);self.assertEqual(status,200)
  status,d=self.call(boss,'device',{'customer_id':c['id'],'name':'測試電腦','cpu':'Ryzen 5','ram':'16GB'},csrf);self.assertEqual(status,200)
  company={'id':c['id'],'customer_type':'company','company_name':'測試公司','name':'對接人','line':'testline','line_id':'test_id'}
  self.assertEqual(self.call(boss,'customer',{**company,'company_name':''},csrf)[0],400)
  self.assertEqual(self.call(boss,'customer',company,csrf)[0],200)
  customer=next(a for a in self.call(boss,'state')[1]['customers'] if a['id']==c['id']);self.assertEqual(customer['company_name'],'測試公司');self.assertEqual(customer['name'],'對接人')
  secret={'windows_password':'  Win 密碼  ','accounts':[{'label':'Microsoft','username':'qa@example.test','password':'account-secret-test'}]}
  self.assertEqual(self.call(boss,'device',{'id':d['id'],'customer_id':c['id'],'name':'測試電腦','anydesk':'123 456 789','credentials':secret},csrf)[0],200)
  self.assertEqual(self.call(boss,'device/secrets',{'id':d['id']},csrf)[1],secret)
  self.assertEqual(self.call(boss,'device/secrets',{'id':d['id']})[0],403)
  data=self.call(boss,'state')[1];device=next(a for a in data['devices'] if a['id']==d['id']);self.assertEqual(device['anydesk'],'123 456 789');self.assertNotIn('windows_password',json.dumps(data))
  self.assertNotIn(b'account-secret-test',self.call(boss,'export')[1])
  with server.db() as con:
   encrypted=con.execute('SELECT encrypted FROM device_secrets WHERE device_id=?',(d['id'],)).fetchone()[0];self.assertNotIn('account-secret-test',encrypted);self.assertNotIn('Windows',encrypted)
  import os,subprocess,sqlite3
  with tempfile.TemporaryDirectory() as backup_dir:
   backup=Path(backup_dir)/'backup.sqlite3';env={**os.environ,'DATA_DIR':str(server.DATA),'PYTHONPATH':os.pathsep.join(filter(None,[str(ROOT),os.environ.get('PYTHONPATH','')]))}
   result=subprocess.run([sys.executable,str(ROOT/'server.py'),'--backup',str(backup)],env=env,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)
   self.assertTrue(backup.with_suffix('.key').exists())
   cbackup=sqlite3.connect(backup)
   encrypted_backup=cbackup.execute('SELECT encrypted FROM device_secrets WHERE device_id=?',(d['id'],)).fetchone()[0];cbackup.close()
   from cryptography.fernet import Fernet
   self.assertEqual(json.loads(Fernet(backup.with_suffix('.key').read_bytes()).decrypt(encrypted_backup.encode())),secret)
  self.assertEqual(self.call(boss,'device',{'id':d['id'],'customer_id':c['id'],'name':'測試電腦修改'},csrf)[0],200)
  self.assertEqual(self.call(boss,'device/secrets',{'id':d['id']},csrf)[1],secret)
  repair={'device_id':d['id'],'date':'2026-10-19','status':'維修中','problem':'無法開機','items':['硬體維修 → 更換 SSD'],'notes':'已換新硬碟','amount':1500,'paid':False}
  status,r=self.call(boss,'repair',repair,csrf);self.assertEqual(status,200)
  self.assertEqual(self.call(boss,'repair',{**repair,'amount':-1},csrf)[0],400)
  self.assertEqual(self.call(boss,'repair',{**repair,'date':'bad-date'},csrf)[0],400)
  repair.update(id=r['id'],status='已結案',paid=True);self.assertEqual(self.call(boss,'repair',repair,csrf)[0],200)
  qr=self.call(boss,'qr?id='+d['id']);self.assertEqual(qr[0],200);self.assertIn(b'<svg',qr[1])
  data=self.call(boss,'state')[1];self.assertEqual(data['repairs'][0]['paid'],1);self.assertEqual(len(data['repairs']),1)
  self.assertEqual(self.call(boss,'export')[0],200)
  tech,t=self.client('worker');viewer,v=self.client('reader')
  self.assertEqual(self.call(viewer,'customer',{'name':'forbidden'},v)[0],403)
  self.assertEqual(self.call(tech,'categories',{'categories':{}},t)[0],403)
  self.assertEqual(self.call(tech,'user',{'username':'hacker'},t)[0],403)
  self.assertEqual(self.call(viewer,'export')[0],403)
  self.assertEqual(self.call(viewer,'device/secrets',{'id':d['id']},v)[0],403)
  self.assertEqual(self.call(tech,'device/secrets',{'id':d['id']},t)[1],secret)
  self.assertEqual(self.call(tech,'customer',{'name':'allowed'},t)[0],200)
  self.assertEqual(self.call(boss,'user',{'id':'boss','username':'boss','name':'boss','role':'viewer','active':True},csrf)[0],400)
  self.assertEqual(self.call(boss,'categories',{'categories':{'軟體安裝':['Adobe','Office','轟天雷','新項目']}},csrf)[0],200)
  for op,token in [(tech,t),(viewer,v)]:
   self.assertEqual(self.call(op,'customer/archive',{'id':c['id'],'archived':True},token)[0],403)
   self.assertEqual(self.call(op,'customer/delete',{'id':c['id'],'confirm':'DELETE'},token)[0],403)
  self.assertEqual(self.call(boss,'customer/archive',{'id':c['id'],'archived':True},csrf)[0],200)
  data=self.call(boss,'state')[1];self.assertEqual(next(a for a in data['customers'] if a['id']==c['id'])['archived'],1)
  self.assertEqual(len(data['devices']),1);self.assertEqual(len(data['repairs']),1)
  self.assertEqual(self.call(boss,'device',{'customer_id':c['id'],'name':'封存後不可新增'},csrf)[0],400)
  self.assertEqual(self.call(boss,'customer/delete',{'id':c['id'],'confirm':'DELETE'},csrf)[0],400)
  self.assertEqual(self.call(boss,'customer',{'id':c['id'],'name':'小明修改姓名'},csrf)[0],200)
  self.assertEqual(next(a for a in self.call(boss,'state')[1]['customers'] if a['id']==c['id'])['archived'],1)
  self.assertEqual(self.call(boss,'customer/archive',{'id':c['id'],'archived':False},csrf)[0],200)
  self.assertEqual(next(a for a in self.call(boss,'state')[1]['customers'] if a['id']==c['id'])['archived'],0)
  status,empty=self.call(boss,'customer',{'name':'誤建客戶'},csrf);self.assertEqual(status,200)
  self.assertEqual(self.call(boss,'customer/delete',{'id':empty['id']},csrf)[0],400)
  self.assertEqual(self.delete_customer(boss,csrf,empty['id'])[0],200)
  self.assertNotIn(empty['id'],[a['id'] for a in self.call(boss,'state')[1]['customers']])
  for op,token in [(tech,t),(viewer,v)]:
   self.assertEqual(self.call(op,'device/archive',{'id':d['id'],'archived':True},token)[0],403)
   self.assertEqual(self.call(op,'device/delete',{'id':d['id'],'confirm':'DELETE'},token)[0],403)
  self.assertEqual(self.call(boss,'device/archive',{'id':d['id'],'archived':True},csrf)[0],200)
  data=self.call(boss,'state')[1];self.assertEqual(next(a for a in data['devices'] if a['id']==d['id'])['archived'],1)
  self.assertEqual(len(data['repairs']),1);self.assertEqual(self.call(boss,'qr?id='+d['id'])[0],200)
  self.assertEqual(self.call(boss,'repair',{**repair,'id':''},csrf)[0],400)
  self.assertEqual(self.call(boss,'device/delete',{'id':d['id'],'confirm':'DELETE'},csrf)[0],409)
  self.assertEqual(self.call(boss,'device/archive',{'id':d['id'],'archived':False},csrf)[0],200)
  self.assertEqual(next(a for a in self.call(boss,'state')[1]['devices'] if a['id']==d['id'])['archived'],0)
  status,unused=self.call(boss,'device',{'customer_id':c['id'],'name':'誤建電腦'},csrf);self.assertEqual(status,200)
  self.assertEqual(self.call(boss,'device/delete',{'id':unused['id']},csrf)[0],400)
  self.assertEqual(self.call(boss,'device/delete',{'id':unused['id'],'confirm':'DELETE'},csrf)[0],200)
  self.assertNotIn(unused['id'],[a['id'] for a in self.call(boss,'state')[1]['devices']])
  self.assertEqual(self.call(boss,'qr?id='+unused['id'])[0],404)
  self.assertEqual(self.call(boss,'logout',{},csrf)[0],200);self.assertEqual(self.call(boss,'state')[0],401)
  with server.db() as con:self.assertEqual(con.execute('SELECT count(*) FROM repairs').fetchone()[0],1)
class MigrationTest(unittest.TestCase):
 def test_existing_database(self):
  import sqlite3
  old=server.DATA
  with tempfile.TemporaryDirectory() as tmp:
   try:
    server.DATA=Path(tmp)
    with sqlite3.connect(server.DATA/'repair.sqlite3') as c:
     c.execute("CREATE TABLE customers(id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',line TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '')")
     c.execute("INSERT INTO customers VALUES('legacy','原有客戶','0912345678','','備註')")
    c.close()
    server.initialize();server.initialize()
    with server.db() as c:
     row=c.execute("SELECT * FROM customers WHERE id='legacy'").fetchone();self.assertEqual(row['archived'],0);self.assertEqual(row['phone'],'0912345678');self.assertEqual(row['customer_type'],'personal');self.assertEqual(row['company_name'],'')
   finally:server.DATA=old
if __name__=='__main__':unittest.main()
