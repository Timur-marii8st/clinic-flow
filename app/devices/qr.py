"""Short-lived opaque QR for synthetic bookings. No credentials in logs/URLs."""
import base64
from io import BytesIO
import hashlib
import json
import re
import secrets
import time

import qrcode
from app.auth.store import actor_id, audit
from app.domain.queue_policy import QueueError

TTL = 24 * 3600
PREFIX = 'CF1:'
FORMAT = re.compile(r'CF1:[A-Za-z0-9_-]{43}')


def hash_value(value):
    return hashlib.sha256(value.encode()).hexdigest()


class QRService:
    def __init__(self,queue,clock=time.time):
        self.queue=queue
        self.clock=clock

    def issue(self,booking_code):
        code=PREFIX+secrets.token_urlsafe(32)
        # Generate before committing; a rendering error must not replace the prior token.
        image=qrcode.make(code,box_size=8,border=4)
        buf=BytesIO()
        image.save(buf,format='PNG')
        expires=int(self.clock())+TTL
        with self.queue.connection(write=True) as con:
            row=con.execute('SELECT * FROM tickets WHERE booking_code=?',(booking_code.strip().upper(),)).fetchone()
            if not row or row['status']!='booked':
                raise QueueError('Запись не найдена или уже обработана',409)
            if con.execute('SELECT 1 FROM mis_appointments WHERE ticket_id=? AND conflict IS NOT NULL',(row['id'],)).fetchone():
                raise QueueError('Уточните запись у регистратора',409)
            con.execute('''INSERT INTO qr_credentials(ticket_id,token_hash,expires_at) VALUES (?,?,?)
                ON CONFLICT(ticket_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at,
                consumed_at=NULL,request_hash=NULL,response=NULL''',(row['id'],hash_value(code),expires))
            audit(con,'qr_issue','ticket',row['id'])
        return {'qr_text':code,'png_base64':base64.b64encode(buf.getvalue()).decode(),
                'expires_at':expires}

    def redeem(self,code,request_key):
        if not FORMAT.fullmatch(code) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_key):
            raise QueueError('Некорректный или просроченный QR-код',422)
        request_hash=hash_value(actor_id()+'\x00'+request_key)
        with self.queue.connection(write=True) as con:
            cred=con.execute('SELECT * FROM qr_credentials WHERE token_hash=?',(hash_value(code),)).fetchone()
            if not cred or cred['expires_at']<=int(self.clock()):
                raise QueueError('Некорректный или просроченный QR-код',409)
            if cred['consumed_at'] is not None:
                if cred['request_hash']==request_hash:
                    return json.loads(cred['response'])
                raise QueueError('QR-код уже использован',409)
            row=self.queue._ticket(con,cred['ticket_id'])
            if not row or row['status']!='booked':
                raise QueueError('Запись уже обработана',409)
            result=self.queue._check_in(con,row)
            con.execute('UPDATE qr_credentials SET consumed_at=?,request_hash=?,response=? WHERE ticket_id=?',
                        (int(self.clock()),request_hash,json.dumps(result,ensure_ascii=False),row['id']))
            audit(con,'qr_redeem','ticket',row['id'])
            return result
