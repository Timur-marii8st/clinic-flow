'use strict';
// Separate admin and enrollment UI; all privileges are enforced by the server.
(function () {
  const root=document.getElementById('app');
  const path=location.pathname;
  const el=(tag,value)=>{const e=document.createElement(tag); if(value!==undefined)e.textContent=value;return e;};
  const input=(label,placeholder='',type='text')=>{const e=el('input');e.type=type;e.placeholder=placeholder;e.setAttribute('aria-label',label);return e;};
  const select=(label,values)=>{const e=el('select');e.setAttribute('aria-label',label);for(const [value,name] of values){const o=el('option',name);o.value=value;e.append(o);}return e;};
  const btn=(label,fn)=>{const e=el('button',label);e.type='button';e.addEventListener('click',fn);return e;};
  const panel=(heading)=>{const p=el('section');p.className='panel';p.append(el('h2',heading));return p;};
  const link=(url,label)=>{const a=el('a',label);a.href=url;return a;};
  const row=()=>{const e=el('div');e.className='row';return e;};
  const alert=(e,s,error=false)=>{e.textContent=s;e.className='notice'+(error?' error':'');};
  async function call(url,body) {
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(8000),
      ...(body!==undefined?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{} throw new Error(typeof detail==='string'?detail:'Ошибка доступа или связи');}
    return response.json();
  }
  async function pair() {
    root.replaceChildren(el('h1','Подключение устройства'));
    const p=panel('Одноразовый код администратора'),code=input('Код подключения','','password'),msg=el('p');
    msg.setAttribute('role','status');code.autocomplete='off';code.maxLength=128;
    const r=row(),send=btn('Подключить',async()=>{
      send.disabled=true;try{const data=await call('/auth/pair',{pairing_code:code.value.trim()});code.value='';location.replace(data.path);}
      catch(e){alert(msg,e.message,true);}finally{send.disabled=false;}
    });
    r.append(code,send);p.append(r,msg,link('/auth/login?next=/staff','Вход сотрудника через SSO'));root.append(p);
  }
  async function admin() {
    root.replaceChildren(el('h1','Управление Clinic Flow'));
    const msg=el('p');msg.setAttribute('role','status');root.append(msg);
    const userPanel=panel('Сотрудники и роли'), users=el('div'), form=row();
    const sub=input('OIDC subject','Идентификатор sub из IdP'),label=input('Отображаемое имя','Условное имя сотрудника');
    const role=select('Роль',[['doctor','Врач'],['registrar','Регистратор'],['analyst','Аналитик'],['admin','Администратор']]);
    const assigned=input('Кабинеты врача','101,102'),enabled=select('Состояние',[['1','Активен'],['0','Отключён']]);
    const save=btn('Сохранить сотрудника',async()=>{save.disabled=true;try{
      await call('/api/admin/users',{subject:sub.value.trim(),label:label.value.trim(),role:role.value,
          rooms:role.value==='doctor'?assigned.value.split(',').map(x=>x.trim()).filter(Boolean):[],enabled:enabled.value==='1'});
      alert(msg,'Сотрудник сохранён; прежние сессии отозваны.');await refresh();
    }catch(e){alert(msg,e.message,true);}finally{save.disabled=false;}});
    form.append(sub,label,role,assigned,enabled,save);userPanel.append(form,users);root.append(userPanel);
    const devicePanel=panel('Терминалы и табло'), devices=el('div'), deviceForm=row(),devLabel=input('Название устройства','Табло 101');
    const kind=select('Тип устройства',[['board','Табло'],['kiosk','Терминал']]),room=select('Привязка кабинета',[['','Общее / без привязки']]);
    const codeOut=el('p');codeOut.setAttribute('role','status');
    const roomList=await call('/api/rooms');for(const v of roomList){const o=el('option',v.label);o.value=v.id;room.append(o);}
    kind.addEventListener('change',()=>{room.disabled=kind.value==='kiosk';if(room.disabled)room.value='';});
    const create=btn('Создать код подключения',async()=>{create.disabled=true;codeOut.textContent='';try{
      const d=await call('/api/admin/devices',{label:devLabel.value,kind:kind.value,room_id:room.value||null});
      alert(codeOut,'Код (10 минут, одно использование): '+d.pairing_code);await refresh();
    }catch(e){alert(msg,e.message,true);}finally{create.disabled=false;}});
    deviceForm.append(devLabel,kind,room,create);devicePanel.append(deviceForm,codeOut,devices);root.append(devicePanel);
    const auditPanel=panel('Последние события аудита'), auditList=el('div');
    auditPanel.append(btn('Обновить',()=>refresh().catch(e=>alert(msg,e.message,true))),auditList);root.append(auditPanel);
    async function refresh() {
      const [ul,dl,al]=await Promise.all(['/api/admin/users','/api/admin/devices','/api/admin/audit'].map(x=>call(x)));
      users.replaceChildren();for(const u of ul){const r=row();r.append(el('span',u.label+' · '+u.role+' · '+(u.enabled?'активен':'отключён')+' · '+u.rooms.join(', ')),
        btn('Изменить',()=>{sub.value=u.subject;label.value=u.label;role.value=u.role;assigned.value=u.rooms.join(',');enabled.value=u.enabled?'1':'0';sub.focus();}));users.append(r);}
      devices.replaceChildren();for(const d of dl){const r=row();r.append(el('span',d.label+' · '+d.kind+' · '+(d.room_id||'общий')+' · '+d.status+(d.online?' · online':'')));
        if(!d.revoked)r.append(btn('Отозвать',async()=>{if(!confirm('Отозвать доступ устройства?'))return;try{await call('/api/admin/devices/'+d.id+'/revoke',{});await refresh();}catch(e){alert(msg,e.message,true);}}));
        devices.append(r);}
      auditList.replaceChildren();for(const a of al){const r=el('p',new Date(a.created_at).toLocaleString('ru-RU')+' · '+a.actor+' · '+a.action+' · '+a.object_type+': '+a.object_id);auditList.append(r);}
    }
    await refresh();
  }
  async function init() {
    if(path==='/pair'){await pair();return;}
    let identity;
    try{identity=await call('/api/me');}catch{identity=null;}
    if(identity?.mode==='demo')return;
    const nav=document.querySelector('header nav');
    if(identity?.kind==='staff'){
      if(identity.role==='admin')nav.append(link('/admin','Администрирование'));
      nav.append(btn('Выйти',async()=>{try{await call('/auth/logout',{});location.replace('/');}catch(e){alert(root,e.message,true);}}));
    }else if(!identity){nav.append(link('/auth/login?next=/staff','Вход сотрудника'));}
    if(path==='/admin')await admin();
    if(identity?.kind==='device'){
      const beat=async()=>{try{await call('/api/devices/heartbeat',{});}catch{/* board refresh detects expired access */}};
      await beat();
      const timer=setInterval(beat,30000);
      addEventListener('pagehide',()=>clearInterval(timer),{once:true});
    }
  }
  init().catch(e=>root.replaceChildren(el('h1','Не удалось загрузить интерфейс'),el('p',e.message)));
})();
