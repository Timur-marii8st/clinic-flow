"""Real Chromium: QR issuance/scanning, local WAV playback, stale screen recovery."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory() as tmp:
        env={**os.environ,'CLINIC_DB_PATH':str(Path(tmp)/'test.db'),'CLINIC_STAFF_PASSWORD':'','CLINIC_MODE':'demo'}
        with subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],
                              cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
            try:
                for _ in range(100):
                    try:
                        if httpx.get(base+'/health',timeout=.4).status_code==200:break
                    except httpx.HTTPError:pass
                    if server.poll() is not None:raise RuntimeError('Server exited')
                    time.sleep(.1)
                else:raise RuntimeError('Server did not start')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_BIN'),headless=True,args=['--no-sandbox'])
                    normal=browser.new_context()
                    staff=normal.new_page(); kiosk=normal.new_page()
                    board_context=browser.new_context()
                    board=board_context.new_page()
                    errors=[]
                    for page in (staff,kiosk,board):
                        page.on('pageerror',lambda error:errors.append(str(error)))
                    staff.goto(base+'/staff')
                    staff.get_by_text('Выдать QR-код записи',exact=True).click()
                    staff.get_by_label('Код записи для QR').fill('DEMO-101')
                    staff.get_by_role('button',name='Создать QR-код').click()
                    expect(staff.locator('.qr-code-text')).to_have_text(__import__('re').compile(r'^CF1:[A-Za-z0-9_-]{43}$'))
                    qr=staff.locator('.qr-code-text').inner_text()
                    staff.wait_for_function("document.querySelector('.qr-result img')?.naturalWidth > 0")
                    kiosk.goto(base+'/kiosk?device=1')
                    expect(kiosk.get_by_label('Код записи')).to_be_focused()
                    kiosk.get_by_label('Код записи').fill(qr)
                    kiosk.get_by_label('Код записи').press('Enter')
                    expect(kiosk.locator('.ticket')).to_have_text('A-0001')
                    expect(kiosk.get_by_label('Код записи')).to_have_value('')
                    kiosk.get_by_role('button',name='Готово',exact=True).click()
                    expect(kiosk.locator('.ticket')).to_have_count(0)
                    expect(staff.get_by_text('A-0001 · Ожидает')).to_be_visible()
                    # Preserve real decoding/playback while counting buffer starts.
                    board.add_init_script("""window.audioStarts=0;
                        const start=AudioBufferSourceNode.prototype.start;
                        AudioBufferSourceNode.prototype.start=function(...args){window.audioStarts++;return start.apply(this,args);};""")
                    board.goto(base+'/board?room=101&device=1')
                    board.evaluate("""window.clipNames=[];const play=ClinicAudio.prototype.play;
                        ClinicAudio.prototype.play=function(name,run){window.clipNames.push(name);return play.call(this,name,run);};""")
                    expect(board.get_by_text('Кабинет 101',exact=True)).to_be_visible()
                    board.get_by_role('button',name='Включить озвучку').click()
                    expect(board.get_by_text('Локальное аудио готово')).to_be_visible(timeout=12000)
                    board.wait_for_function("window.audioStarts >= 1")
                    staff.get_by_role('button',name='Вызвать следующего').click()
                    expect(board.locator('.active .ticket')).to_have_text('A-0001')
                    board.wait_for_function("window.clipNames.includes('intro') && window.audioStarts >= 2",timeout=10000)
                    board_context.set_offline(True)
                    expect(board.get_by_text('Нет актуальных данных. Обратитесь к сотруднику.')).to_be_visible(timeout=18000)
                    expect(board.locator('.active .ticket')).to_have_count(0)
                    assert board.evaluate('cfTelemetry().state')=='stale'
                    before=board.evaluate("clipNames.filter(n=>n==='intro').length")
                    board_context.set_offline(False)
                    expect(board.locator('.active .ticket')).to_have_text('A-0001',timeout=18000)
                    assert board.evaluate("clipNames.filter(n=>n==='intro').length")==before
                    assert board.evaluate('cfTelemetry().state')=='ready'
                    assert not errors,errors
                    browser.close()
                    print('PASS: QR PNG + HID-style check-in, local decoded WAV playback, stale display and reconnect without re-announcement')
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()


if __name__=='__main__':
    main()
