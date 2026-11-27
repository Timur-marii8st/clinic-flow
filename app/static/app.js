'use strict';
const app = document.getElementById('app');
const mode = location.pathname;
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
  title('Добро пожаловать','Подтвердите запись или получите демонстрационный талон.');
  const rooms = await api('/api/rooms'), grid = node('section',undefined,'grid'), msg = notice(), result = node('section',undefined,'panel');
  result.hidden = true; let busy = false, pending = null;
  async function submit(path,body) {
    if (busy) return;
    busy = true; grid.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const t = await api(path,body); pending = null;
      result.replaceChildren(node('h2','Ваш талон'),node('div',t.code,'ticket'),node('p',`Кабинет ${t.room_id} · ${labels[t.status] || t.status}`),node('p','Следите за табло.','muted'));
      result.hidden = false; message(msg,'Талон зарегистрирован.');
    } catch (e) { result.hidden = true; message(msg,e.message + (pending ? ' Повторите запрос для того же кабинета.' : ''),true); }
    finally { busy = false; grid.querySelectorAll('button').forEach(b => b.disabled = false); }
  }
  const p = node('article',undefined,'panel'), q = node('article',undefined,'panel'), input = node('input');
  input.placeholder = 'DEMO-101'; input.maxLength = 32; input.required = true; input.setAttribute('aria-label','Код записи');
  const form = node('form',undefined,'row'), check = node('button','Подтвердить'); check.type = 'submit'; form.append(input,check);
  form.addEventListener('submit',e => { e.preventDefault(); submit('/api/check-in',{booking_code:input.value}); });
  p.append(node('h2','Я записан'),node('p','Тестовые коды: DEMO-101, DEMO-102, DEMO-103.','muted'),form);
  const select = roomSelect(rooms), row = node('div',undefined,'row');
  row.append(select,button('Получить талон',() => {
    if (pending && pending.room_id !== select.value && !confirm('Предыдущий запрос мог создать талон. Выдать новый в другой кабинет?')) return;
    if (!pending || pending.room_id !== select.value) pending = {room_id:select.value,request_key:uid()};
    submit('/api/walk-in',pending);
  }));
  q.append(node('h2','Без записи'),node('p','В реальной клинике маршрут определяет регистратура.','muted'),row);
  grid.append(p,q); app.append(grid,msg,result);
}
async function staff() {
  title('Рабочее место врача','Приоритет наступившей записи, FIFO для остальных; долго ожидающие получают преимущество. Правила только для демо.');
  const identity = await api('/api/me');
  const rooms = await api('/api/rooms'), select = roomSelect(rooms), panel = node('section',undefined,'panel'), bar = node('div',undefined,'row'), msg = notice(), rows = node('div'), absent = node('div'), sync = node('p','Подключение…','muted');
  let busy = false, seq = 0, connected = false;
  const call = button('Вызвать следующего',() => mutate(`/api/staff/${select.value}/call-next`,{})); call.disabled = true;
  bar.append(select,call,link('/analytics','Аналитика')); panel.append(bar,sync,rows,absent,msg); app.append(panel);
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
        const choices = t.status === 'called' ? [['Начать','start'],['Повторить','repeat'],['Вернуть в очередь','requeue'],['Не явился','no_show']] : t.status === 'serving' ? [['Завершить','finish']] : [['Отменить','cancel']];
        const url = `/api/staff/${room}/tickets/${t.id}`;
        for (const [label,action] of choices) actions.append(button(label,() => {
          if (['no_show','cancel','requeue'].includes(action) && !confirm('Подтвердить действие?')) return;
          mutate(`${url}/action`,{action,expected_version:t.version});
        },!['start','finish'].includes(action)));
        const target = roomSelect(rooms.filter(v => v.id !== room)); target.setAttribute('aria-label','Куда перенести');
        const move = button('Перенести',() => { if (confirm(`Перенести ${t.code} в кабинет ${target.value}?`)) mutate(`${url}/transfer`,{target_room:target.value,expected_version:t.version}); },true);
        actions.append(target,move); r.append(desc,actions); rows.append(r);
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
  async function mutate(path,body) {
    if (busy || !connected) return;
    busy = true; ++seq; call.disabled = true; panel.querySelectorAll('button').forEach(b => b.disabled = true); select.disabled = true;
    const key = uid();
    try {
      try { await api(path,body,key); }
      catch (e) {
        // Retry only transport failures, with exactly the same command key.
        if (!(e instanceof TypeError || e.name === 'TimeoutError' || e.name === 'AbortError')) throw e;
        await api(path,body,key);
      }
      message(msg,'Действие выполнено');
    } catch(e) { message(msg,e.message + ' Проверьте актуальную очередь перед повтором.',true); }
    finally { busy = false; select.disabled = false; await refresh(); }
  }
  select.addEventListener('change',() => { connected = false; rows.replaceChildren(); absent.replaceChildren(); call.disabled = true; refresh(); });
  const booking = node('details',undefined,'panel'); booking.append(node('summary','Создать синтетическую запись на время'));
  const bookingForm = node('form',undefined,'row'), code = node('input'), date = node('input'), dest = roomSelect(rooms), submit = node('button','Создать запись'), result = notice();
  code.placeholder = 'DEMO-TEST'; code.required = true; code.maxLength = 32; code.setAttribute('aria-label','Тестовый код записи'); date.type = 'datetime-local'; date.required = true; date.setAttribute('aria-label','Время приёма'); submit.type = 'submit';
  bookingForm.append(code,dest,date,submit); booking.append(bookingForm,result); if (['demo','admin','registrar'].includes(identity.role)) app.append(booking);
  bookingForm.addEventListener('submit',async e => { e.preventDefault(); submit.disabled = true; try { await api('/api/staff/demo-appointments',{booking_code:code.value,room_id:dest.value,scheduled_at:new Date(date.value).toISOString()}); message(result,'Запись создана. Подтвердите её в терминале.'); } catch(e) { message(result,e.message,true); } finally { submit.disabled = false; } });
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
  title('Приглашение на приём','Следите за номером своего талона.');
  const grid = node('section',undefined,'grid'), sync = node('p','Подключение…','muted'); grid.id='board-grid'; sync.setAttribute('role','status');
  const filter = new URLSearchParams(location.search).get('room'); let loading = false, dirty = false, voice = false, initialized = false; const seen = new Map();
  if ('speechSynthesis' in window && 'SpeechSynthesisUtterance' in window) {
    const v = button('Включить озвучку',() => { voice = !voice; v.textContent = voice ? 'Выключить озвучку' : 'Включить озвучку'; if (voice) say('Озвучка включена'); else speechSynthesis.cancel(); },true); app.append(v);
  }
  function say(text) { const u = new SpeechSynthesisUtterance(text); u.lang = 'ru-RU'; speechSynthesis.speak(u); }
  app.append(sync,grid);
  async function refresh() {
    if (loading) { dirty = true; return; } loading = true;
    try {
      let data = await api('/api/board'); if (filter) data = data.filter(r => r.room_id === filter);
      grid.replaceChildren();
      for (const r of data) {
        const p = node('article',undefined,'panel' + (r.state === 'called' ? ' active' : ''));
        p.append(node('h2',r.label),node('div',r.code || '—','ticket'),node('p',r.state === 'called' ? 'Проходите в кабинет' : r.state === 'serving' ? 'Идёт приём' : 'Ожидайте вызова','status')); grid.append(p);
        if (voice && initialized && r.state === 'called' && seen.get(r.room_id) !== r.call_version) say(`Талон ${r.code.replace('A-','А, ').split('').join(' ')}. ${r.label}`);
        seen.set(r.room_id,r.call_version);
      }
      initialized = true; if (!data.length) grid.append(node('p','Кабинет не найден.'));
      sync.textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU'); sync.className = 'muted';
    } catch (e) { grid.replaceChildren(); initialized = false; if (voice) speechSynthesis.cancel(); sync.textContent = 'Нет связи с сервером. Обратитесь к сотруднику.'; sync.className = 'stale'; }
    finally { loading = false; if (dirty) { dirty = false; refresh(); } }
  }
  await refresh(); subscribe(refresh);
}
async function analytics() {
  title('Аналитика потока','По последним 100 завершённым этапам каждого кабинета. Это не прогноз и не SLA.');
  const grid = node('section',undefined,'grid'), msg = notice(); app.append(grid,msg); let busy = false;
  async function refresh() {
    if (busy) return; busy = true;
    try {
      const data = await api('/api/analytics'); grid.replaceChildren();
      for (const r of data) {
        const p = node('article',undefined,'panel'); p.append(node('h2',r.label),node('div',String(r.waiting),'ticket'),node('p','В очереди','muted'),node('p',`Завершено: ${r.completed} · Неявки: ${r.no_shows} · Переводы: ${r.transfers}`),node('p',`Медиана ожидания: ${minutes(r.median_wait_seconds)} (n=${r.wait_samples})`),node('p',`Медиана приёма: ${minutes(r.median_service_seconds)} (n=${r.service_samples})`)); grid.append(p);
      }
      message(msg,'Обновлено ' + new Date().toLocaleTimeString('ru-RU'));
    } catch(e) { grid.replaceChildren(); message(msg,e.message,true); } finally { busy = false; }
  }
  await refresh(); subscribe(refresh);
}
app.replaceChildren();
(mode === '/kiosk' ? kiosk() : mode === '/staff' ? staff() : mode === '/board' ? board() : mode === '/analytics' ? analytics() : ['/admin','/pair'].includes(mode) ? Promise.resolve() : Promise.resolve(home())).catch(e => app.replaceChildren(node('h1','Не удалось загрузить интерфейс'),node('p',e.message,'error')));
