"""永暘電腦內部維修管理。Run behind a private LAN/WireGuard boundary."""
import argparse, datetime as dt, hashlib, hmac, http.cookies, io, json, os, re, secrets, sqlite3, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import qrcode
import qrcode.image.svg
import vault
import photos

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', ROOT / 'data'))
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.50.123:8080').rstrip('/')
COOKIE_SECURE = BASE_URL.startswith('https://')
DEFAULT_CATEGORIES = {'硬體維修':['更換 SSD','更換記憶體','更換電源供應器','更換主機板'], '系統處理':['重灌系統','驅動程式安裝','資料備份','病毒清除'], '軟體安裝':['Adobe','Office','轟天雷'], '清潔保養':['除塵清潔','更換散熱膏','風扇保養']}
STATUSES = ['待檢測','維修中','待取件','已完成待送回','已結案']

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def db():
    con = sqlite3.connect(DATA / 'repair.sqlite3', timeout=20, factory=Connection)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','tech','viewer')),active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(ip TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',line TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,code TEXT UNIQUE NOT NULL,customer_id TEXT NOT NULL REFERENCES customers(id),name TEXT NOT NULL,type TEXT NOT NULL DEFAULT '桌上型電腦',cpu TEXT NOT NULL DEFAULT '',ram TEXT NOT NULL DEFAULT '',storage TEXT NOT NULL DEFAULT '',gpu TEXT NOT NULL DEFAULT '',os TEXT NOT NULL DEFAULT '',serial TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS repairs(id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id),date TEXT NOT NULL,status TEXT NOT NULL,problem TEXT NOT NULL,items TEXT NOT NULL,notes TEXT NOT NULL,amount INTEGER NOT NULL CHECK(amount>=0),paid INTEGER NOT NULL,author TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL);
        ''')
        for table in ('customers','devices'):
            if 'archived' not in {r['name'] for r in con.execute('PRAGMA table_info('+table+')')}:
                con.execute('ALTER TABLE '+table+' ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        for table,column,definition in [('customers','customer_type',"TEXT NOT NULL DEFAULT 'personal'"),('customers','company_name',"TEXT NOT NULL DEFAULT ''"),('customers','line_id',"TEXT NOT NULL DEFAULT ''"),('customers','address',"TEXT NOT NULL DEFAULT ''"),('devices','anydesk',"TEXT NOT NULL DEFAULT ''")]:
            if column not in {r['name'] for r in con.execute('PRAGMA table_info('+table+')')}:
                con.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+definition)
        con.execute('CREATE TABLE IF NOT EXISTS device_secrets(device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,encrypted TEXT NOT NULL)')
        vault.initialize_key(DATA,con)
        con.execute('CREATE TABLE IF NOT EXISTS device_photos(id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,image BLOB NOT NULL,thumbnail BLOB NOT NULL,caption TEXT NOT NULL,is_cover INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL)')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS device_photo_cover ON device_photos(device_id) WHERE is_cover=1')
        con.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('categories', json.dumps(DEFAULT_CATEGORIES, ensure_ascii=False)))

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def uid(): return secrets.token_urlsafe(18)
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def password_hash(s):
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(s.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
def password_valid(s, stored):
    salt, expected = stored.split(':')
    return hmac.compare_digest(hashlib.scrypt(s.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex(), expected)
def audit(con, user, action, target): con.execute('INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)',(now(),user['username'],action,target))
def clean(x, key, limit=500, required=False):
    v=x.get(key,'')
    if not isinstance(v,str) or len(v)>limit: raise ValueError('欄位格式錯誤：'+key)
    v=v.strip()
    if key in ('id','customer_id','device_id') and v and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',v): raise ValueError('資料識別碼格式不正確')
    if required and not v: raise ValueError('請填寫必要欄位：'+key)
    return v

def customer_delete_info(con, id):
    customer=con.execute('SELECT * FROM customers WHERE id=?',(id,)).fetchone()
    if not customer:return None
    devices=[dict(r) for r in con.execute('SELECT * FROM devices WHERE customer_id=? ORDER BY id',(id,))]
    repairs=[dict(r) for r in con.execute('SELECT r.* FROM repairs r JOIN devices d ON r.device_id=d.id WHERE d.customer_id=? ORDER BY r.id',(id,))]
    photos_list=[dict(r) for r in con.execute('SELECT p.id,p.device_id,p.caption,p.is_cover,p.created FROM device_photos p JOIN devices d ON p.device_id=d.id WHERE d.customer_id=? ORDER BY p.id',(id,))]
    secrets_list=[dict(r) for r in con.execute('SELECT s.device_id,s.encrypted FROM device_secrets s JOIN devices d ON s.device_id=d.id WHERE d.customer_id=? ORDER BY s.device_id',(id,))]
    name=customer['company_name'] if customer['customer_type']=='company' else customer['name']
    snapshot=digest(json.dumps([dict(customer),devices,repairs,photos_list,secrets_list],ensure_ascii=False,sort_keys=True))
    return {'id':id,'name':name,'counts':{'devices':len(devices),'repairs':len(repairs),'photos':len(photos_list),'credentials':len(secrets_list)},'snapshot':snapshot}

class Handler(BaseHTTPRequestHandler):
    server_version='YongYang/1.0'
    def log_message(self, fmt, *args): pass
    def send(self, obj, status=200, mime='application/json; charset=utf-8', cookie=None, download=None):
        body=json.dumps(obj,ensure_ascii=False).encode() if mime.startswith('application/json') else obj if isinstance(obj,bytes) else obj.encode()
        self.send_response(status); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY'); self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie: self.send_header('Set-Cookie',cookie)
        if download: self.send_header('Content-Disposition','attachment; filename="'+download+'"')
        self.end_headers(); self.wfile.write(body)
    def cookie(self,token,maxage): return 'yy_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(maxage)+('; Secure' if COOKIE_SECURE else '')
    def session(self, con):
        try:
            c=http.cookies.SimpleCookie(self.headers.get('Cookie','')); token=c['yy_session'].value
        except (KeyError,http.cookies.CookieError): return None
        return con.execute('SELECT u.*,s.csrf FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>? AND u.active=1',(digest(token),time.time())).fetchone()
    def do_GET(self): self.handle_request(False)
    def do_POST(self): self.handle_request(True)
    def handle_request(self, write):
        try: self.dispatch(write)
        except ValueError as e: self.send({'error':str(e)},400)
        except sqlite3.IntegrityError: self.send({'error':'資料重複或關聯不存在，請重新整理後再試。'},409)
        except Exception:
            import traceback; traceback.print_exc()
            self.send({'error':'伺服器暫時無法處理，請稍後再試。'},500)
    def dispatch(self, write):
        path=urlsplit(self.path).path
        if not write and (path=='/' or path.startswith('/device/')):
            return self.send((ROOT/'static/index.html').read_bytes(),mime='text/html; charset=utf-8')
        if not write and path in ['/app.js','/style.css']:
            return self.send((ROOT/'static'/path[1:]).read_bytes(),mime='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
        x={}
        if write:
            # Require same-origin writes even for login; never trust forwarded headers.
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'): return self.send({'error':'來源不符'},403)
            if self.headers.get('Sec-Fetch-Site')=='cross-site': return self.send({'error':'來源不符'},403)
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>(photos.MAX_ENCODED+10000 if path=='/api/photo/upload' else 100000): return self.send({'error':'資料過大'},413)
            try: x=json.loads(self.rfile.read(size))
            except (ValueError,UnicodeDecodeError): raise ValueError('資料格式錯誤')
            if not isinstance(x,dict): raise ValueError('資料格式錯誤')
        with db() as con:
            if path=='/api/login' and write:
                ip=self.client_address[0]; att=con.execute('SELECT * FROM attempts WHERE ip=?',(ip,)).fetchone()
                if att and att['until']>time.time() and att['count']>=8: return self.send({'error':'嘗試次數過多，請在 15 分鐘後再試。'},429)
                username=clean(x,'username',80,True); pw=clean(x,'password',200,True)
                u=con.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
                if not u or not password_valid(pw,u['password']):
                    count=att['count']+1 if att and att['until']>time.time() else 1
                    con.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(ip,count,time.time()+900)); return self.send({'error':'帳號或密碼不正確'},401)
                con.execute('DELETE FROM attempts WHERE ip=?',(ip,)); con.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
                token=uid()+uid(); csrf=uid(); con.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(token),u['id'],csrf,time.time()+43200))
                audit(con,u,'登入',u['id']); return self.send({'ok':True},cookie=self.cookie(token,43200))
            u=self.session(con)
            if not u: return self.send({'error':'請先登入'},401)
            if write and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),u['csrf']): return self.send({'error':'登入已失效，請重新整理'},403)
            if path=='/api/logout' and write:
                c=http.cookies.SimpleCookie(self.headers.get('Cookie','')); con.execute('DELETE FROM sessions WHERE token=?',(digest(c['yy_session'].value),)); return self.send({'ok':True},cookie=self.cookie('',0))
            if path=='/api/state' and not write:
                return self.send({'me':{k:u[k] for k in ['id','username','name','role','csrf']},'customers':[{k:r[k] for k in r.keys() if k!='line_url'} for r in con.execute('SELECT * FROM customers ORDER BY name')],'devices':[dict(r) for r in con.execute('SELECT * FROM devices ORDER BY created DESC')],'repairs':[{**dict(r),'items':json.loads(r['items'])} for r in con.execute('SELECT * FROM repairs ORDER BY date DESC,created DESC')],'categories':json.loads(con.execute("SELECT value FROM settings WHERE key='categories'").fetchone()[0]),'users':[dict(r) for r in con.execute('SELECT id,username,name,role,active FROM users ORDER BY name')] if u['role']=='admin' else [],'photos':[dict(r) for r in con.execute('SELECT id,device_id,caption,is_cover,created FROM device_photos ORDER BY created,id')],'baseUrl':BASE_URL})
            if path=='/api/export' and not write:
                if u['role']!='admin': return self.send({'error':'僅限管理員'},403)
                data={t:[dict(r) for r in con.execute('SELECT * FROM '+t)] for t in ['customers','devices','repairs','settings']}
                data['customers']=[{k:v for k,v in row.items() if k!='line_url'} for row in data['customers']]
                return self.send(json.dumps({'exported':now(),'data':data},ensure_ascii=False,indent=2).encode(),mime='application/octet-stream',download='yongyang-records.json')
            if path=='/api/photo' and not write:
                id=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                column='thumbnail' if parse_qs(urlsplit(self.path).query).get('thumb')==['1'] else 'image'
                photo=con.execute('SELECT '+column+' AS image FROM device_photos WHERE id=?',(id,)).fetchone()
                if not photo:return self.send({'error':'找不到照片'},404)
                return self.send(photo['image'],mime='image/jpeg')
            if path=='/api/qr' and not write:
                id=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                if not con.execute('SELECT id FROM devices WHERE id=?',(id,)).fetchone(): return self.send({'error':'找不到電腦'},404)
                image=qrcode.make(BASE_URL+'/device/'+id,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=4)
                out=io.BytesIO(); image.save(out); return self.send(out.getvalue(),mime='image/svg+xml',download='YY-'+id+'.svg' if 'download' in parse_qs(urlsplit(self.path).query) else None)
            if not write: return self.send({'error':'找不到頁面'},404)
            if path=='/api/password':
                if not password_valid(clean(x,'old',200,True),u['password']): raise ValueError('原密碼不正確')
                new=clean(x,'new',200,True)
                if len(new)<12: raise ValueError('密碼至少 12 個字元')
                con.execute('UPDATE users SET password=? WHERE id=?',(password_hash(new),u['id'])); con.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],));audit(con,u,'變更密碼',u['id']);return self.send({'ok':True},cookie=self.cookie('',0))
            if u['role']=='viewer': return self.send({'error':'唯讀人員無法修改資料'},403)
            if path=='/api/photo/upload':
                did=clean(x,'device_id',80,True);caption=clean(x,'caption',200)
                image,thumbnail=photos.process(x.get('image'))
                con.execute('BEGIN IMMEDIATE')
                if not con.execute('SELECT id FROM devices WHERE id=?',(did,)).fetchone(): return self.send({'error':'找不到電腦'},404)
                count=con.execute('SELECT count(*) FROM device_photos WHERE device_id=?',(did,)).fetchone()[0]
                if count>=photos.MAX_PHOTOS:raise ValueError('每台電腦最多 10 張照片，請先移除不需要的照片')
                id=uid();con.execute('INSERT INTO device_photos VALUES(?,?,?,?,?,?,?)',(id,did,image,thumbnail,caption,int(count==0),now()));audit(con,u,'新增電腦照片',did);return self.send({'ok':True,'id':id})
            if path in ('/api/photo/cover','/api/photo/delete'):
                id=clean(x,'id',80,True)
                con.execute('BEGIN IMMEDIATE')
                photo=con.execute('SELECT device_id,is_cover FROM device_photos WHERE id=?',(id,)).fetchone()
                if not photo:return self.send({'error':'找不到照片'},404)
                did=photo['device_id']
                if path.endswith('/cover'):
                    con.execute('UPDATE device_photos SET is_cover=0 WHERE device_id=?',(did,));con.execute('UPDATE device_photos SET is_cover=1 WHERE id=?',(id,));audit(con,u,'設定電腦封面',did)
                else:
                    if x.get('confirm')!='DELETE':raise ValueError('請確認移除照片')
                    con.execute('DELETE FROM device_photos WHERE id=?',(id,))
                    if photo['is_cover']:con.execute('UPDATE device_photos SET is_cover=1 WHERE id=(SELECT id FROM device_photos WHERE device_id=? ORDER BY created,id LIMIT 1)',(did,))
                    audit(con,u,'移除電腦照片',did)
                return self.send({'ok':True})
            if path=='/api/customer':
                id=clean(x,'id',80) or uid(); vals=[clean(x,'name',100,True),clean(x,'phone',60),clean(x,'line',500),clean(x,'notes',3000)]
                customer_type=clean(x,'customer_type',20) or 'personal'
                if customer_type not in ('personal','company'): raise ValueError('請選擇個人或公司')
                company_name=clean(x,'company_name',200,customer_type=='company') if customer_type=='company' else ''
                con.execute('INSERT INTO customers(id,name,phone,line,notes) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,phone=excluded.phone,line=excluded.line,notes=excluded.notes',(id,*vals));con.execute('UPDATE customers SET customer_type=?,company_name=? WHERE id=?',(customer_type,company_name,id))
                if 'line_id' in x:con.execute('UPDATE customers SET line_id=? WHERE id=?',(clean(x,'line_id',200),id))
                if 'address' in x:con.execute('UPDATE customers SET address=? WHERE id=?',(clean(x,'address',500),id))
                audit(con,u,'儲存客戶',id);return self.send({'ok':True,'id':id})
            if path=='/api/customer/delete-preview':
                if u['role']!='admin':return self.send({'error':'僅限管理員'},403)
                id=clean(x,'id',80,True);con.execute('BEGIN');info=customer_delete_info(con,id)
                return self.send(info if info else {'error':'找不到客戶'},200 if info else 404)
            if path in ('/api/customer/archive','/api/customer/delete'):
                if u['role']!='admin': return self.send({'error':'封存、還原與永久刪除僅限管理員'},403)
                id=clean(x,'id',80,True)
                if path.endswith('/delete'):con.execute('BEGIN IMMEDIATE')
                if not con.execute('SELECT id FROM customers WHERE id=?',(id,)).fetchone(): return self.send({'error':'找不到客戶'},404)
                if path.endswith('/archive'):
                    archived=x.get('archived')
                    if type(archived)!=bool: raise ValueError('封存狀態不正確')
                    con.execute('UPDATE customers SET archived=? WHERE id=?',(int(archived),id));audit(con,u,'封存客戶' if archived else '還原客戶',id)
                else:
                    if x.get('confirm')!='DELETE': raise ValueError('請確認永久刪除')
                    info=customer_delete_info(con,id)
                    if clean(x,'confirm_name',200,True)!=info['name']:raise ValueError('輸入的客戶名稱不符，未刪除任何資料')
                    if x.get('snapshot')!=info['snapshot']:return self.send({'error':'客戶或名下資料已變更，未刪除。請取消並重新開啟刪除視窗，確認最新範圍。'},409)
                    con.execute('DELETE FROM repairs WHERE device_id IN (SELECT id FROM devices WHERE customer_id=?)',(id,))
                    con.execute('DELETE FROM devices WHERE customer_id=?',(id,))
                    con.execute('DELETE FROM customers WHERE id=?',(id,));audit(con,u,'永久刪除客戶及所有名下資料',id)
                return self.send({'ok':True})
            if path=='/api/device':
                id=clean(x,'id',80) or uid(); old=con.execute('SELECT * FROM devices WHERE id=?',(id,)).fetchone()
                cid=clean(x,'customer_id',80,True)
                if not con.execute('SELECT id FROM customers WHERE id=?',(cid,)).fetchone(): raise ValueError('請先建立客戶')
                if (not old or cid!=old['customer_id']) and con.execute('SELECT archived FROM customers WHERE id=?',(cid,)).fetchone()['archived']: raise ValueError('請先還原此客戶，再新增或移轉電腦')
                vals=[clean(x,k,3000 if k=='notes' else 200,k=='name') for k in ['name','type','cpu','ram','storage','gpu','os','serial','notes']]
                anydesk=clean(x,'anydesk',100) if 'anydesk' in x else old['anydesk'] if old else ''
                if 'credentials' in x:vault.validate(x['credentials'])
                code=old['code'] if old else 'YY-'+dt.datetime.now().strftime('%y')+'-'+secrets.token_hex(3).upper()
                con.execute('INSERT INTO devices(id,code,customer_id,name,type,cpu,ram,storage,gpu,os,serial,notes,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,name=excluded.name,type=excluded.type,cpu=excluded.cpu,ram=excluded.ram,storage=excluded.storage,gpu=excluded.gpu,os=excluded.os,serial=excluded.serial,notes=excluded.notes',(id,code,cid,*vals,old['created'] if old else now()));con.execute('UPDATE devices SET anydesk=? WHERE id=?',(anydesk,id))
                if 'credentials' in x:vault.save(con,DATA,id,x['credentials'])
                audit(con,u,'儲存電腦',id);return self.send({'ok':True,'id':id})
            if path=='/api/device/secrets':
                id=clean(x,'id',80,True)
                if not con.execute('SELECT id FROM devices WHERE id=?',(id,)).fetchone(): return self.send({'error':'找不到電腦'},404)
                credentials=vault.load(con,DATA,id);audit(con,u,'查看電腦帳號密碼',id);return self.send(credentials)
            if path in ('/api/device/archive','/api/device/delete'):
                if u['role']!='admin': return self.send({'error':'封存、還原與永久刪除僅限管理員'},403)
                id=clean(x,'id',80,True)
                if not con.execute('SELECT id FROM devices WHERE id=?',(id,)).fetchone(): return self.send({'error':'找不到電腦'},404)
                if path.endswith('/archive'):
                    archived=x.get('archived')
                    if type(archived)!=bool: raise ValueError('封存狀態不正確')
                    con.execute('UPDATE devices SET archived=? WHERE id=?',(int(archived),id));audit(con,u,'封存電腦' if archived else '還原電腦',id)
                else:
                    if x.get('confirm')!='DELETE': raise ValueError('請確認永久刪除')
                    if con.execute('SELECT 1 FROM repairs WHERE device_id=? LIMIT 1',(id,)).fetchone(): return self.send({'error':'此電腦有維修紀錄，無法永久刪除；請改用封存，保留維修歷史。'},409)
                    con.execute('DELETE FROM devices WHERE id=?',(id,));audit(con,u,'永久刪除電腦',id)
                return self.send({'ok':True})
            if path=='/api/repair':
                id=clean(x,'id',80) or uid(); did=clean(x,'device_id',80,True); date=clean(x,'date',10,True);dt.date.fromisoformat(date)
                if not con.execute('SELECT id FROM devices WHERE id=?',(did,)).fetchone(): raise ValueError('找不到電腦')
                old=con.execute('SELECT * FROM repairs WHERE id=?',(id,)).fetchone()
                if not old and con.execute('SELECT archived FROM devices WHERE id=?',(did,)).fetchone()['archived']: raise ValueError('請先還原這台電腦，再新增維修紀錄')
                if old and old['device_id']!=did: raise ValueError('無法變更紀錄所屬電腦')
                status=clean(x,'status',20,True)
                if status not in STATUSES: raise ValueError('進度不正確')
                amount=x.get('amount');paid=x.get('paid');items=x.get('items')
                if type(amount)!=int or amount<0 or amount>999999999: raise ValueError('金額請輸入有效的新臺幣整數')
                if type(paid)!=bool: raise ValueError('收款狀態不正確')
                if not isinstance(items,list) or len(items)>100 or any(not isinstance(v,str) or len(v)>300 for v in items): raise ValueError('維修項目格式不正確')
                con.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET date=excluded.date,status=excluded.status,problem=excluded.problem,items=excluded.items,notes=excluded.notes,amount=excluded.amount,paid=excluded.paid',(id,did,date,status,clean(x,'problem',3000,True),json.dumps(items,ensure_ascii=False),clean(x,'notes',5000),amount,int(paid),old['author'] if old else u['name'],old['created'] if old else now()));audit(con,u,'儲存維修紀錄',id);return self.send({'ok':True,'id':id})
            if path=='/api/categories':
                if u['role']!='admin': return self.send({'error':'僅限管理員'},403)
                cats=x.get('categories')
                if not isinstance(cats,dict) or len(cats)>60: raise ValueError('分類格式不正確')
                for k,v in cats.items():
                    if not k.strip() or len(k)>80 or not isinstance(v,list) or len(v)>200 or any(not isinstance(s,str) or not s.strip() or len(s)>100 for s in v) or len(set(v))!=len(v): raise ValueError('分類名稱不能空白或重複')
                con.execute("BEGIN IMMEDIATE")
                if 'expected' in x and x['expected']!=json.loads(con.execute("SELECT value FROM settings WHERE key='categories'").fetchone()[0]): return self.send({'error':'選單已被其他操作更新，請取消並重新整理後再試，避免覆蓋新資料。'},409)
                con.execute("UPDATE settings SET value=? WHERE key='categories'",(json.dumps(cats,ensure_ascii=False),));audit(con,u,'更新維修選單','categories');return self.send({'ok':True})
            if path=='/api/user':
                if u['role']!='admin': return self.send({'error':'僅限管理員'},403)
                id=clean(x,'id',80) or uid();old=con.execute('SELECT * FROM users WHERE id=?',(id,)).fetchone();role=clean(x,'role',20,True)
                if role not in ['admin','tech','viewer']: raise ValueError('角色不正確')
                active=x.get('active',True)
                if type(active)!=bool: raise ValueError('帳號狀態不正確')
                if id==u['id'] and (role!='admin' or not active): raise ValueError('不能停用或降級自己的管理員帳號')
                pw=clean(x,'password',200,not bool(old))
                if pw and len(pw)<12: raise ValueError('密碼至少 12 個字元')
                username=clean(x,'username',80,True)
                if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,80}',username): raise ValueError('帳號請用至少 3 個英數字或 _ . -')
                con.execute('INSERT INTO users VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,password=excluded.password,role=excluded.role,active=excluded.active',(id,username,clean(x,'name',100,True),password_hash(pw) if pw else old['password'],role,int(active)))
                con.execute('DELETE FROM sessions WHERE user_id=?',(id,));audit(con,u,'更新使用者',id);return self.send({'ok':True})
            return self.send({'error':'找不到操作'},404)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--init-admin',action='store_true');p.add_argument('--backup');p.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'));p.add_argument('--port',type=int,default=int(os.environ.get('PORT','8080')));a=p.parse_args();initialize()
    if a.init_admin:
        import getpass
        with db() as con:
            if con.execute('SELECT 1 FROM users').fetchone(): raise SystemExit('已有帳號，請從管理介面新增人員。')
            username=input('管理員帳號（英數字，至少 3 字）：').strip();pw=getpass.getpass('密碼（至少 12 字）：');confirm=getpass.getpass('再次輸入密碼：')
            if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,80}',username) or len(pw)<12 or pw!=confirm: raise SystemExit('帳號或密碼格式錯誤，或兩次密碼不一致。')
            con.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(uid(),username,'管理員',password_hash(pw),'admin'));print('管理員已建立。')
    elif a.backup:
        target=Path(a.backup)
        if target.exists(): raise SystemExit('備份檔已存在，請使用新的檔名。')
        with db() as con, sqlite3.connect(target) as dest: con.backup(dest)
        key_target=target.with_suffix('.key')
        if key_target.exists(): raise SystemExit('金鑰備份檔已存在，請使用新的檔名。')
        key_target.write_bytes((DATA/'credentials.key').read_bytes());os.chmod(key_target,0o600)
        print('備份完成（含配對加密金鑰）：'+str(target))
    else:
        print('永暘電腦服務啟動：'+BASE_URL,flush=True);ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
