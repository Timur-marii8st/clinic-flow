'use strict';
const app = document.getElementById('app');
const mode = location.pathname;
let deviceState='starting', deviceLast=0, deviceAudio=false;
window.cfTelemetry=()=>({page:['/board','/kiosk'].includes(mode)?mode.slice(1):'unknown',state:deviceState,age_seconds:deviceLast?Math.min(86400,Math.max(0,Math.floor((performance.now()-deviceLast)/1000))):null,audio_ready:deviceAudio,client_version:'0.7.0'});
function deviceFresh(){deviceLast=performance.now();deviceState='ready';}
function deviceStale(){deviceState='stale';}
if(new URLSearchParams(location.search).get('device')==='1'&&['/board','/kiosk'].includes(mode))document.body.classList.add('device-view');
const labels = { waiting:'Ожидает', called:'Приглашён', serving:'На приёме', done:'Завершён', no_show:'Не явился', cancelled:'Отменён', booked:'Записан' };
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
const button = (text, fn, secondary = false) => { const b = node('button', text, secondary ? 'secondary' : ''); b.type = 'button'; b.addEventListener('click', fn); return b; };
const minutes = s => s === null ? '—' : `${(s / 60).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} мин`;
const clock = s => s ? new Date(s).toLocaleTimeString('ru-RU', { hour:'2-digit', minute:'2-digit' }) : '—';
const uid = () => { const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); return Array.from(bytes, x => x.toString(16).padStart(2,'0')).join(''); };
async function api(path, body, requestKey) {
  const res = await fetch(path, { cache:'no-store', credentials:'same-origin', signal:AbortSignal.timeout(5000), ...(body !== undefined ? { method:'POST', headers:{'Content-Type':'application/json', ...(requestKey ? {'Idempotency-Key':requestKey} : {})}, body:JSON.stringify(body) } : {}) });
  const data = await res.json();
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте введённые данные');
  return data;
}
function message(el, text, error = false) { el.textContent = text; el.className = 'notice' + (error ? ' error' : ''); }
function title(a,b) { app.append(node('h1',a), node('p',b,'muted')); }
function roomSelect(rooms) { const s = node('select'); s.setAttribute('aria-label','Кабинет'); for (const r of rooms) { const o = node('option',r.label); o.value = r.id; s.append(o); } return s; }
function notice() { const el = node('div','','notice'); el.setAttribute('role','status'); return el; }
function link(url,text,cls) { const a = node('a',text,cls); a.href = url; return a; }
// Push notifications contain no patient information. Reconnect fetches a fresh
// snapshot. A 2s fallback keeps legacy/unstable clients usable. A watchdog
// also catches half-open connections; server heartbeats normally arrive at 10s.
function subscribe(refresh) {
  let es, last = 0;
  if ('EventSource' in window) {
    es = new EventSource('/api/events');
    const trigger = () => { last = Date.now(); refresh(); };
    es.addEventListener('refresh',trigger);
    es.addEventListener('heartbeat',trigger);
    es.onerror = () => { last = 0; refresh(); };
  }
  const timer = setInterval(() => { if (!es || es.readyState !== EventSource.OPEN || Date.now() - last > 13000) refresh(); },2000);
  window.addEventListener('pagehide', () => { clearInterval(timer); es?.close(); }, { once:true });
  window.addEventListener('pageshow', e => { if (e.persisted) location.reload(); });
}
function home() {
  title('Электронная очередь','Четыре интерфейса одного локального стенда. Откройте их в разных вкладках.');
  const grid = node('section',undefined,'grid');
  for (const [url,h,d] of [['/kiosk','Терминал пациента','Выдача талона и подтверждение записи.'],['/staff','Рабочее место врача','Вызов, маршрутизация и восстановление неявок.'],['/board','Информационное табло','Вызовы по кабинетам, опциональная озвучка.'],['/analytics','Аналитика','Медианы ожидания и длительности приёма.']]) {
    const p = node('article',undefined,'panel'); p.append(node('h2',h),node('p',d,'muted'),link(url,'Открыть','button')); grid.append(p);
  }
  app.append(grid);
}
async function kiosk() {
  document.body.classList.add('kiosk-page');
  title('Добро пожаловать','Подтвердите запись кодом или QR, либо обратитесь к регистратору.');
  const rooms = await api('/api/rooms'), grid = node('section',undefined,'grid'), msg = notice(), result = node('section',undefined,'panel kiosk-result'), connection=node('p','Связь с сервером','muted');
  deviceFresh();
  result.hidden = true; let busy = false, pending = null, qrPending = null, clearTimer = null, probing=false, online=true, connectionEpoch=0;
  function clearResult(){clearTimeout(clearTimer);result.hidden=true;result.replaceChildren();message(msg,'');}
  async function submit(path,body) {
    if (busy || !online) return false;
    busy = true; const startedEpoch=connectionEpoch; clearResult(); grid.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const t = await api(path,body);
      if(startedEpoch!==connectionEpoch||!online){message(msg,'Проверьте связь и повторите тот же запрос.',true);return false;}
      pending = null; qrPending=null; deviceFresh();
      result.replaceChildren(node('h2','Ваш талон'),node('div',t.code,'ticket'),node('p',`Кабинет ${t.room_id} · ${labels[t.status] || t.status}`),node('p','Следите за табло.','muted'),button('Готово',()=>{clearResult();input.focus();},true));
      result.hidden = false; message(msg,'Талон зарегистрирован.');clearTimer=setTimeout(()=>{clearResult();input.focus();},45000);
      return true;
    } catch (e) { result.hidden = true; message(msg,e.message + (pending||qrPending ? ' Повторите тот же запрос после проверки связи.' : ''),true); return false; }
    finally { busy = false; grid.querySelectorAll('button').forEach(b => b.disabled = !online); }
  }
  const p = node('article',undefined,'panel kiosk-card'), q = node('article',undefined,'panel kiosk-card'), input = node('input');
  input.placeholder = 'Код или QR'; input.maxLength = 64; input.required = true; input.autocomplete='off';input.spellcheck=false;input.setAttribute('autocapitalize','off');input.setAttribute('aria-label','Код записи');
  const form = node('form',undefined,'row'), check = node('button','Подтвердить'); check.type = 'submit'; form.append(input,check);
  form.addEventListener('submit',async e => {
    e.preventDefault();const value=input.value.trim();
    if(!value||busy)return;
    let ok;
    if(value.startsWith('CF1:')){
      if(!qrPending||qrPending.code!==value)qrPending={code:value,request_key:uid()};
      ok=await submit('/api/check-in/qr',qrPending);
    }else{
      qrPending=null;ok=await submit('/api/check-in',{booking_code:value});
    }
    if(ok||value.startsWith('CF1:')){input.value='';input.focus();}else input.select();
  });
  p.append(node('div','01','step-number'),node('h2','Я записан'),node('p','Поднесите QR к USB-сканеру или введите код записи. Тестовые коды: DEMO-101, DEMO-102, DEMO-103.','muted'),form);
  const select = roomSelect(rooms), row = node('div',undefined,'row');
  row.append(select,button('Получить талон',() => {
    if (pending && pending.room_id !== select.value && !confirm('Предыдущий запрос мог создать талон. Выдать новый в другой кабинет?')) return;
    if (!pending || pending.room_id !== select.value) pending = {room_id:select.value,request_key:uid()};
    submit('/api/walk-in',pending);
  }));
  q.append(node('div','02','step-number'),node('h2','Без записи'),node('p','В реальной клинике маршрут определяет регистратура.','muted'),row);
  grid.append(p,q); app.append(connection,grid,msg,result);input.focus();
  function lost(){if(online)connectionEpoch++;online=false;deviceStale();connection.textContent='Нет связи. Обратитесь к регистратору.';connection.className='stale';clearResult();grid.querySelectorAll('button').forEach(b=>b.disabled=true);}
  async function probe(){
    if(probing)return;probing=true;
    try{
      const current=await api('/api/rooms');
      if(JSON.stringify(current)!==JSON.stringify(rooms)){
        const prior=select.value;rooms.splice(0,rooms.length,...current);
        select.replaceChildren(...Array.from(roomSelect(rooms).options));
        if(rooms.some(r=>r.id===prior))select.value=prior;
      }
      online=true;deviceFresh();connection.textContent='Связь с сервером';connection.className='muted';
      if(!busy)grid.querySelectorAll('button').forEach(b=>b.disabled=false);
    }catch{lost();}finally{probing=false;}
  }
  const timer=setInterval(probe,5000);
  const watchdog=setInterval(()=>{if(performance.now()-deviceLast>=15000&&online)lost();},1000);
  addEventListener('offline',lost);addEventListener('online',probe);
  addEventListener('pagehide',()=>{clearInterval(timer);clearInterval(watchdog);clearTimeout(clearTimer);},{once:true});
}
async function staff() {
  title('Рабочее место врача','Приоритет наступившей записи, FIFO для остальных; долго ожидающие получают преимущество. Правила только для демо.');
  const identity = await api('/api/me');
  const rooms = await api('/api/rooms'), select = roomSelect(rooms), panel = node('section',undefined,'panel'), bar = node('div',undefined,'row'), msg = notice(), rows = node('div'), absent = node('div'), sync = node('p','Подключение…','muted');
  let busy = false, seq = 0, connected = false;
  const call = button('Вызвать следующего',() => mutate(`/api/staff/${select.value}/call-next`,{})); call.disabled = true;
  const routePanel=node('section',undefined,'panel');routePanel.hidden=true;
  bar.append(select,call,link('/analytics','Аналитика')); panel.append(bar,sync,rows,absent,msg); app.append(panel,routePanel);
  async function refresh() {
    if (busy) return;
    const s = ++seq, room = select.value;
    try {
      const [list, missed] = await Promise.all([api(`/api/staff/${room}`),api(`/api/staff/${room}/no-shows`)]);
      if (s !== seq || room !== select.value) return;
      connected = true; sync.textContent = 'Связь с сервером · ' + new Date().toLocaleTimeString('ru-RU'); sync.className = 'muted';
      call.disabled = !list.some(t => t.eligible) || list.some(t => ['called','serving'].includes(t.status)); rows.replaceChildren(); absent.replaceChildren();
      if (!list.length) rows.append(node('p','Очередь пуста','muted'));
      for (const t of list) {
        const r = node('div',undefined,'list-row'), desc = node('div'), actions = node('div',undefined,'row');
        const details = [t.source === 'appointment' ? 'По записи' : 'Без записи', t.scheduled_at ? `Запись ${clock(t.scheduled_at)}` : '', `Прибыл ${clock(t.checked_in_at)}`, t.status === 'waiting' && !t.eligible ? 'Время ещё не наступило' : ''].filter(Boolean).join(' · ');
        desc.append(node('strong',`${t.code} · ${labels[t.status]}`),node('div',details,'muted'));
        if(t.route)desc.append(node('p',t.route.label+' · '+t.route.active_label+' · '+(t.route.completed_count+1)+'/'+t.route.total_steps,'muted'));
        const choices = t.status === 'called' ? [['Начать','start'],['Повторить','repeat'],['Вернуть в очередь','requeue'],['Не явился','no_show']] : t.status === 'serving' ? (t.route?[]:[['Завершить','finish']]) : [['Отменить','cancel']];
        const url = `/api/staff/${room}/tickets/${t.id}`;
        for (const [label,action] of choices) actions.append(button(label,() => {
          if (['no_show','cancel','requeue'].includes(action) && !confirm('Подтвердить действие?')) return;
          mutate(`${url}/action`,{action,expected_version:t.version});
        },!['start','finish'].includes(action)));
        const target = roomSelect(rooms.filter(v => v.id !== room)); target.setAttribute('aria-label','Куда перенести');
        const move = button('Перенести',() => { if (confirm(`Перенести ${t.code} в кабинет ${target.value}?`)) mutate(`${url}/transfer`,{target_room:target.value,expected_version:t.version}); },true);
        if(!t.route){
          actions.append(target,move);
          if(t.status==='waiting'&&['demo','admin','registrar'].includes(identity.role))actions.append(button('Назначить маршрут',()=>openAttach(t,room),true));
        }else if(t.status==='serving')actions.append(button('Следующий этап',()=>openRoute(t,room)));
        r.append(desc,actions); rows.append(r);
      }
      if (missed.length) {
        absent.append(node('h2','Недавние неявки'));
        for (const t of missed) {
          const r = node('div',undefined,'list-row');
          r.append(node('strong',t.code),button('Вернуть в очередь',() => mutate(`/api/staff/${room}/tickets/${t.id}/action`,{action:'requeue',expected_version:t.version}),true)); absent.append(r);
        }
      }
    } catch (e) { if (s !== seq) return; connected = false; sync.textContent = 'Нет связи: данные скрыты, действия недоступны.'; sync.className = 'stale'; rows.replaceChildren(); absent.replaceChildren(); call.disabled = true; }
  }
  function routeDialog(titleText){
    routePanel.replaceChildren(node('h2',titleText),button('Закрыть',()=>{routePanel.hidden=true;routePanel.replaceChildren();},true));
    routePanel.hidden=false;
    const content=node('div');routePanel.append(content);return content;
  }
  async function openAttach(t,room){
    const content=routeDialog('Назначить маршрут: '+t.code);
    try{
      const templates=await api('/api/routes/templates'),available=[];
      for(const template of templates)for(const step of template.plan.steps)if(!step.requires.length&&step.rooms.includes(room))available.push({template,step});
      if(!available.length){content.append(node('p','Нет подходящих шаблонов. Создайте согласованный демонстрационный шаблон.'));return;}
      const choice=node('select');choice.setAttribute('aria-label','Шаблон и первый этап');
      available.forEach((v,i)=>{const o=node('option',v.template.label+' v'+v.template.version+' · '+v.step.label);o.value=String(i);choice.append(o);});
      content.append(choice,button('Подтвердить маршрут',async()=>{
        const v=available[Number(choice.value)];
        if(!v||select.value!==room||!confirm('Назначить указанный маршрут?'))return;
        if(await mutate('/api/staff/'+room+'/tickets/'+t.id+'/route/attach',{template_id:v.template.id,template_version:v.template.version,step_id:v.step.id,expected_version:t.version}))routePanel.hidden=true;
      }));
    }catch(e){content.append(node('p',e.message,'error'));}
  }
  async function openRoute(t,room){
    const content=routeDialog('Завершение этапа: '+t.code);
    try{
      const url='/api/staff/'+room+'/tickets/'+t.id, data=await api(url+'/route');
      content.append(node('p','Текущий этап: '+data.route.active_label+'. Подтвердите фактическое завершение.'));
      if(data.final){
        content.append(button('Завершить маршрут',async()=>{if(select.value===room&&confirm('Все назначенные этапы выполнены?')&&await mutate(url+'/action',{action:'finish',expected_version:data.version}))routePanel.hidden=true;}));return;
      }
      if(!data.choices.length){content.append(node('p','Нет разрешённого кабинета в ваших правах. Обратитесь к регистратору.'));return;}
      const choice=node('select');choice.setAttribute('aria-label','Следующий кабинет');
      for(const [i,c] of data.choices.entries()){
        const o=node('option',(c.recommended?'Рекомендация · ':'')+c.step_label+' → '+c.room_id+' · '+(c.eta_seconds===null?'Нагрузка '+c.workload:'Оценка '+minutes(c.eta_seconds)));
        o.value=String(i);choice.append(o);
      }
      content.append(node('p','Подсказка по очереди, не медицинский приоритет. Не учитывает будущие записи и перерывы.','muted'),choice,
        button('Завершить и направить',async()=>{
          const c=data.choices[Number(choice.value)];
          if(!c||select.value!==room||!confirm('Текущий этап действительно выполнен?'))return;
          if(await mutate(url+'/route/advance',{step_id:c.step_id,target_room:c.room_id,expected_version:data.version}))routePanel.hidden=true;
        }));
    }catch(e){content.append(node('p',e.message,'error'));}
  }
  async function mutate(path,body) {
    if (busy || !connected) return false;
    let succeeded=false;
    busy = true; ++seq; call.disabled = true; panel.querySelectorAll('button').forEach(b => b.disabled = true); select.disabled = true;
    const key = uid();
    try {
      try { await api(path,body,key); }
      catch (e) {
        // Retry only transport failures, with exactly the same command key.
        if (!(e instanceof TypeError || e.name === 'TimeoutError' || e.name === 'AbortError')) throw e;
        await api(path,body,key);
      }
      message(msg,'Действие выполнено');succeeded=true;
    } catch(e) { message(msg,e.message + ' Проверьте актуальную очередь перед повтором.',true); }
    finally { busy = false; select.disabled = false; await refresh(); }
    return succeeded;
  }
  select.addEventListener('change',() => { routePanel.hidden=true; connected = false; rows.replaceChildren(); absent.replaceChildren(); call.disabled = true; refresh(); });
  const booking = node('details',undefined,'panel'); booking.append(node('summary','Создать синтетическую запись на время'));
  const bookingForm = node('form',undefined,'row'), code = node('input'), date = node('input'), dest = roomSelect(rooms), submit = node('button','Создать запись'), result = notice();
  code.placeholder = 'DEMO-TEST'; code.required = true; code.maxLength = 32; code.setAttribute('aria-label','Тестовый код записи'); date.type = 'datetime-local'; date.required = true; date.setAttribute('aria-label','Время приёма'); submit.type = 'submit';
  bookingForm.append(code,dest,date,submit); booking.append(bookingForm,result); if (['demo','admin','registrar'].includes(identity.role)) app.append(booking);
  bookingForm.addEventListener('submit',async e => { e.preventDefault(); submit.disabled = true; try { await api('/api/staff/demo-appointments',{booking_code:code.value,room_id:dest.value,scheduled_at:new Date(date.value).toISOString()}); message(result,'Запись создана. Подтвердите её в терминале.'); } catch(e) { message(result,e.message,true); } finally { submit.disabled = false; } });
  if(['demo','admin','registrar'].includes(identity.role)){
    const qr=node('details',undefined,'panel');qr.append(node('summary','Выдать QR-код записи'));
    const qrRow=node('form',undefined,'row'),qrInput=node('input'),qrButton=node('button','Создать QR-код'),qrResult=node('div',undefined,'qr-result'),qrMsg=notice();
    qrInput.placeholder='DEMO-101 или MIS-…';qrInput.maxLength=32;qrInput.required=true;qrInput.autocomplete='off';qrInput.setAttribute('aria-label','Код записи для QR');qrButton.type='submit';
    qrRow.append(qrInput,qrButton);qr.append(qrRow,qrMsg,qrResult);app.append(qr);
    let qrTimer;
    qrRow.addEventListener('submit',async e=>{
      e.preventDefault();qrButton.disabled=true;clearTimeout(qrTimer);qrResult.replaceChildren();
      try{
        const d=await api('/api/staff/qr',{booking_code:qrInput.value});
        const img=node('img');img.src='data:image/png;base64,'+d.png_base64;img.alt='Одноразовый QR-код для подтверждения записи';img.width=280;img.height=280;
        const raw=node('code',d.qr_text,'qr-code-text');
        qrResult.append(img,node('p','Действует до '+new Date(d.expires_at*1000).toLocaleString('ru-RU')),node('p','Тестовый код для ручного ввода:'),raw,
          button('Скрыть QR',()=>{qrResult.replaceChildren();message(qrMsg,'');},true));
        qrInput.value='';message(qrMsg,'QR создан. Покажите его пациенту; не публикуйте код.');
        qrTimer=setTimeout(()=>{qrResult.replaceChildren();message(qrMsg,'');},90000);
      }catch(e){message(qrMsg,e.message,true);}finally{qrButton.disabled=false;}
    });
  }
  if(['demo','admin'].includes(identity.role)){
    const editor=node('details',undefined,'panel');editor.append(node('summary','Шаблоны маршрутов'));
    const form=node('form',undefined,'route-editor'), templateId=node('input'),label=node('input'),ver=node('input'),plan=node('textarea'),save=node('button','Сохранить версию шаблона'),messageBox=notice();
    templateId.value='demo-route';templateId.required=true;templateId.setAttribute('aria-label','ID шаблона');
    label.value='Демонстрационный маршрут';label.required=true;label.setAttribute('aria-label','Название шаблона');
    ver.type='number';ver.min='0';ver.step='1';ver.value='0';ver.required=true;ver.setAttribute('aria-label','Текущая версия (0 для нового)');
    plan.rows=10;plan.required=true;plan.setAttribute('aria-label','JSON маршрута');
    plan.value=JSON.stringify({steps:[{id:'start',label:'Вход',rooms:['101']},{id:'a',label:'Процедура A',rooms:['102','103'],requires:['start']},{id:'b',label:'Процедура B',rooms:['103'],requires:['start']},{id:'end',label:'Итог',rooms:['101'],requires:['a','b']}]},null,2);
    save.type='submit';form.append(node('p','ID, название и ожидаемая текущая версия'),templateId,label,ver,plan,save);editor.append(form,messageBox);app.append(editor);
    form.addEventListener('submit',async e=>{e.preventDefault();save.disabled=true;try{
      const d=await api('/api/routes/templates',{id:templateId.value,label:label.value,expected_version:Number(ver.value),plan:JSON.parse(plan.value)});
      ver.value=String(d.version);message(messageBox,'Шаблон сохранён, версия '+d.version);
    }catch(e){message(messageBox,e.message,true);}finally{save.disabled=false;}});
  }
  const config = node('details',undefined,'panel');
  config.append(node('summary','Управление кабинетами (демо)'));
  const configForm = node('form',undefined,'row'), roomCode = node('input'), roomLabel = node('input'), saveRoom = node('button','Сохранить кабинет'), configMsg = notice();
  roomCode.placeholder = 'LAB-1'; roomCode.required = true; roomCode.maxLength = 16; roomCode.pattern = '[A-Za-z0-9_\\-]+'; roomCode.setAttribute('aria-label','Код кабинета');
  roomLabel.placeholder = 'Лаборатория'; roomLabel.required = true; roomLabel.maxLength = 100; roomLabel.setAttribute('aria-label','Название кабинета');
  saveRoom.type = 'submit'; configForm.append(roomCode,roomLabel,saveRoom); config.append(configForm,configMsg); if (['demo','admin'].includes(identity.role)) app.append(config);
  configForm.addEventListener('submit',async e => {
    e.preventDefault(); saveRoom.disabled = true;
    try {
      await api('/api/staff/rooms',{room_id:roomCode.value,label:roomLabel.value});
      const updated = await api('/api/rooms');
      rooms.splice(0,rooms.length,...updated);
      for (const s of [select,dest]) {
        const previous = s.value;
        s.replaceChildren(...Array.from(roomSelect(rooms).options));
        s.value = rooms.some(r => r.id === previous) ? previous : rooms[0].id;
      }
      message(configMsg,'Кабинет сохранён. Другие интерфейсы можно обновить.');
      await refresh();
    } catch(e) { message(configMsg,e.message,true); }
    finally { saveRoom.disabled = false; }
  });
  await refresh(); subscribe(refresh);
}
async function board() {
  document.body.classList.add('board-page');
  title('Приглашение на приём','Следите за номером своего талона.');
  const grid = node('section',undefined,'grid'), sync = node('p','Подключение…','muted');
  grid.id='board-grid';sync.setAttribute('role','status');grid.setAttribute('aria-label','Вызовы пациентов');
  const filter=new URLSearchParams(location.search).get('room');
  let loading=false,dirty=false,initialized=false,epoch=0,stale=false,disposed=false;
  const voiceMsg=node('span','Звук выключен','muted'),controls=node('div',undefined,'row board-controls');
  let voiceBtn;
  const audio=new window.ClinicAudio(error=>{
    deviceAudio=false;voiceMsg.textContent=error;voiceBtn.textContent='Включить озвучку';
  });
  async function toggleAudio(){
    voiceBtn.disabled=true;
    try{
      if(audio.enabled){audio.disable();deviceAudio=false;voiceBtn.textContent='Включить озвучку';voiceMsg.textContent='Звук выключен';}
      else{await audio.enable();deviceAudio=true;voiceBtn.textContent='Выключить озвучку';voiceMsg.textContent='Локальное аудио готово';}
    }catch{audio.disable();deviceAudio=false;voiceMsg.textContent='Локальные WAV не установлены или звук недоступен.';voiceBtn.textContent='Включить озвучку';}
    finally{voiceBtn.disabled=false;}
  }
  voiceBtn=button('Включить озвучку',toggleAudio,true);
  controls.append(voiceBtn,voiceMsg);app.append(controls,sync,grid);
  function invalidate(){
    if(!stale){epoch++;stale=true;}
    deviceStale();grid.replaceChildren();initialized=false;audio.reset();
    sync.textContent='Нет актуальных данных. Обратитесь к сотруднику.';sync.className='stale';
  }
  async function refresh() {
    if(disposed)return;
    if(loading){dirty=true;return;}loading=true;const startedEpoch=epoch;
    try{
      let data=await api('/api/board');
      if(disposed||startedEpoch!==epoch)return;
      if(filter)data=data.filter(r=>r.room_id===filter);
      grid.replaceChildren();
      for(const r of data){
        const p=node('article',undefined,'panel'+(r.state==='called'?' active':''));
        p.append(node('h2',r.label),node('div',r.code||'—','ticket'),node('p',r.state==='called'?'Проходите в кабинет':r.state==='serving'?'Идёт приём':'Ожидайте вызова','status'));grid.append(p);
      }
      audio.sync(data,initialized&&!stale);
      initialized=true;stale=false;deviceFresh();if(!data.length)grid.append(node('p','Кабинет не найден.'));
      sync.textContent='Обновлено '+new Date().toLocaleTimeString('ru-RU');sync.className='muted';
    }catch{if(!disposed)invalidate();}
    finally{loading=false;if(dirty&&!disposed){dirty=false;refresh();}}
  }
  const watchdog=setInterval(()=>{
    if(deviceLast&&performance.now()-deviceLast>=15000){invalidate();refresh();}
  },1000);
  addEventListener('offline',()=>{invalidate();refresh();});
  addEventListener('online',refresh);
  addEventListener('pagehide',()=>{disposed=true;clearInterval(watchdog);audio.disable();},{once:true});
  await refresh();subscribe(refresh);
  if(new URLSearchParams(location.search).get('audio')==='1')toggleAudio();
}
async function analytics() {
  title('Аналитика потока','Наблюдаемая статистика за последние 24 часа. Оценки нагрузки не являются обещанием времени приёма.');
  const grid = node('section',undefined,'grid'), msg = notice(); app.append(grid,msg); let busy = false;
  async function refresh() {
    if (busy) return; busy = true;
    try {
      const [data,report] = await Promise.all([api('/api/analytics'),api('/api/pilot/analytics?hours=24')]); grid.replaceChildren();
      const routeCard=node('article',undefined,'panel');routeCard.append(node('h2','Маршруты · 24 ч'),node('p','Завершено: '+report.routes.completed_sample+' · Активно: '+report.routes.active),node('p','p50 от назначения: '+minutes(report.routes.from_attachment_p50_seconds)),node('p','p90 от назначения: '+minutes(report.routes.from_attachment_p90_seconds)));grid.append(routeCard);
      for (const r of data) {
        const windowRoom=report.rooms.find(x=>x.room_id===r.room_id);
        const p = node('article',undefined,'panel'); p.append(node('h2',r.label),node('div',String(r.waiting),'ticket'),node('p','В очереди','muted'),node('p',`Завершено: ${r.completed} · Неявки: ${r.no_shows} · Переводы: ${r.transfers}`),node('p',`Медиана ожидания: ${minutes(r.median_wait_seconds)} (n=${r.wait_samples})`),node('p',`Медиана приёма: ${minutes(r.median_service_seconds)} (n=${r.service_samples})`)); if(windowRoom){p.append(node('p','Ожидание p90 (24 ч): '+minutes(windowRoom.wait_p90_seconds)),node('p','Наблюдаемое время приёма: '+minutes(windowRoom.observed_service_seconds)),node('p','Оценка для нового пациента: '+minutes(windowRoom.current_estimate.eta_seconds)+' (n='+windowRoom.current_estimate.samples+')'));if(windowRoom.truncated)p.append(node('p','Выборка ограничена','muted'));} grid.append(p);
      }
      message(msg,'Обновлено ' + new Date().toLocaleTimeString('ru-RU'));
    } catch(e) { grid.replaceChildren(); message(msg,e.message,true); } finally { busy = false; }
  }
  await refresh(); subscribe(refresh);
}
app.replaceChildren();
(mode === '/kiosk' ? kiosk() : mode === '/staff' ? staff() : mode === '/board' ? board() : mode === '/analytics' ? analytics() : ['/admin','/pair'].includes(mode) ? Promise.resolve() : Promise.resolve(home())).catch(e => {deviceStale();app.replaceChildren(node('h1','Не удалось загрузить интерфейс'),node('p',e.message,'error'),button('Повторить подключение',()=>location.reload()));});
