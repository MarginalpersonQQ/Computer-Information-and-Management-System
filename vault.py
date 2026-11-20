"""Encrypt retrievable device credentials; never expose them in list/export APIs."""
import json, os
from cryptography.fernet import Fernet

def cipher(data_dir):
    path=data_dir/'credentials.key'
    return Fernet(path.read_bytes())

def initialize_key(data_dir, con):
    path=data_dir/'credentials.key'
    if not path.exists():
        if con.execute('SELECT 1 FROM device_secrets LIMIT 1').fetchone():
            raise RuntimeError('密碼加密金鑰遺失，請從完整備份還原 credentials.key')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
    os.chmod(path,0o600)
    cipher(data_dir)

def validate(value):
    if not isinstance(value,dict): raise ValueError('帳號密碼格式不正確')
    def field(x,k,limit):
        v=x.get(k,'')
        if not isinstance(v,str) or len(v)>limit: raise ValueError('帳號密碼欄位過長或格式不正確')
        return v
    result={'windows_password':field(value,'windows_password',1000),'accounts':[]}
    accounts=value.get('accounts',[])
    if not isinstance(accounts,list) or len(accounts)>30: raise ValueError('自訂帳號最多 30 組')
    for account in accounts:
        if not isinstance(account,dict): raise ValueError('自訂帳號格式不正確')
        row={k:field(account,k,1000 if k=='password' else 200) for k in ('label','username','password')}
        if not any(row.values()):continue
        if not row['label'].strip(): raise ValueError('請填寫自訂帳號的項目名稱')
        result['accounts'].append(row)
    return result

def save(con,data_dir,device_id,value):
    value=validate(value)
    encrypted=cipher(data_dir).encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()
    con.execute('INSERT INTO device_secrets(device_id,encrypted) VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET encrypted=excluded.encrypted',(device_id,encrypted))

def load(con,data_dir,device_id):
    row=con.execute('SELECT encrypted FROM device_secrets WHERE device_id=?',(device_id,)).fetchone()
    return json.loads(cipher(data_dir).decrypt(row['encrypted'].encode())) if row else {'windows_password':'','accounts':[]}
