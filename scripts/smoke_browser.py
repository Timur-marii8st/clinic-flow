"""Optional actual Chromium demo smoke. Requires playwright and chromium.
Starts disposable local DB/server; does not touch existing data.
Run: python scripts/smoke_browser.py
"""
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
base = f'http://127.0.0.1:{port}'
with tempfile.TemporaryDirectory() as tmp:
    env = {**os.environ,'CLINIC_DB_PATH':str(Path(tmp)/'test.db'),'CLINIC_STAFF_PASSWORD':''}
    with subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
        try:
            for _ in range(100):
                try:
                    if httpx.get(base+'/health',timeout=0.4).status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                if server.poll() is not None:
                    raise RuntimeError('Server exited')
                time.sleep(0.1)
            else:
                raise RuntimeError('Server did not start')
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_BIN'),headless=True,args=['--no-sandbox'])
                context = browser.new_context()
                kiosk, staff, board, analytics = [context.new_page() for _ in range(4)]
                errors = []
                for page, route in zip((kiosk,staff,board,analytics),('/kiosk','/staff','/board','/analytics')):
                    page.on('pageerror',lambda error: errors.append(str(error)))
                    page.on('dialog',lambda d:d.accept())
                    page.goto(base+route)
                expect(staff.get_by_text('Очередь пуста')).to_be_visible()
                expect(board.get_by_text('Кабинет 101')).to_be_visible()
                kiosk.get_by_role('button',name='Получить талон').click()
                expect(kiosk.locator('.ticket')).to_have_text('A-0004')
                expect(staff.get_by_text('A-0004 · Ожидает')).to_be_visible(timeout=6000)
                started = time.monotonic()
                staff.get_by_role('button',name='Вызвать следующего').click()
                expect(board.locator('.active .ticket')).to_have_text('A-0004',timeout=5000)
                print(f'Click-to-visible board (one local run): {(time.monotonic()-started)*1000:.0f} ms')
                expect(staff.get_by_role('button',name='Начать',exact=True)).to_be_visible()
                staff.get_by_role('button',name='Начать',exact=True).click()
                expect(staff.get_by_text('A-0004 · На приёме')).to_be_visible()
                staff.get_by_label('Куда перенести').select_option('102')
                staff.get_by_role('button',name='Перенести',exact=True).click()
                expect(staff.get_by_text('Очередь пуста')).to_be_visible()
                staff.get_by_label('Кабинет',exact=True).first.select_option('102')
                expect(staff.get_by_text('A-0004 · Ожидает')).to_be_visible()
                staff.get_by_role('button',name='Вызвать следующего').click()
                expect(board.locator('.active .ticket')).to_have_text('A-0004')
                staff.get_by_role('button',name='Начать',exact=True).click()
                staff.get_by_role('button',name='Завершить',exact=True).click()
                expect(staff.get_by_text('Очередь пуста')).to_be_visible()
                expect(analytics.get_by_text('Завершено: 1 · Неявки: 0 · Переводы: 0')).to_be_visible()
                # Full route via staff UI; default DAG has two independent procedures.
                staff.get_by_label('Кабинет',exact=True).first.select_option('101')
                staff.get_by_text('Шаблоны маршрутов',exact=True).click()
                staff.get_by_role('button',name='Сохранить версию шаблона').click()
                expect(staff.get_by_text('Шаблон сохранён, версия 1')).to_be_visible()
                kiosk.get_by_role('button',name='Получить талон').click()
                expect(kiosk.locator('.ticket')).to_have_text('A-0005')
                expect(staff.get_by_text('A-0005 · Ожидает')).to_be_visible()
                staff.get_by_role('button',name='Назначить маршрут').click()
                staff.get_by_role('button',name='Подтвердить маршрут').click()
                expect(staff.get_by_text('Демонстрационный маршрут · Вход · 1/4')).to_be_visible()
                staff.get_by_role('button',name='Вызвать следующего').click()
                staff.get_by_role('button',name='Начать',exact=True).click()
                staff.get_by_role('button',name='Следующий этап').click()
                staff.get_by_label('Следующий кабинет').select_option(value=staff.get_by_role('option',name=__import__('re').compile('Процедура B.*103')).get_attribute('value'))
                staff.get_by_role('button',name='Завершить и направить').click()
                expect(staff.get_by_text('Очередь пуста')).to_be_visible()
                staff.get_by_label('Кабинет',exact=True).first.select_option('103')
                expect(staff.get_by_text('Демонстрационный маршрут · Процедура B · 2/4')).to_be_visible()
                staff.get_by_role('button',name='Вызвать следующего').click()
                staff.get_by_role('button',name='Начать',exact=True).click()
                staff.get_by_role('button',name='Следующий этап').click()
                staff.get_by_label('Следующий кабинет').select_option(value=staff.get_by_role('option',name=__import__('re').compile('Процедура A.*102')).get_attribute('value'))
                staff.get_by_role('button',name='Завершить и направить').click()
                staff.get_by_label('Кабинет',exact=True).first.select_option('102')
                expect(staff.get_by_text('Демонстрационный маршрут · Процедура A · 3/4')).to_be_visible()
                staff.get_by_role('button',name='Вызвать следующего').click()
                staff.get_by_role('button',name='Начать',exact=True).click()
                staff.get_by_role('button',name='Следующий этап').click()
                staff.get_by_role('button',name='Завершить и направить').click()
                staff.get_by_label('Кабинет',exact=True).first.select_option('101')
                expect(staff.get_by_text('Демонстрационный маршрут · Итог · 4/4')).to_be_visible()
                staff.get_by_role('button',name='Вызвать следующего').click()
                staff.get_by_role('button',name='Начать',exact=True).click()
                staff.get_by_role('button',name='Следующий этап').click()
                staff.get_by_role('button',name='Завершить маршрут').click()
                expect(staff.get_by_text('Очередь пуста')).to_be_visible()
                expect(analytics.get_by_text('Завершено: 1 · Активно: 0')).to_be_visible()
                print('PASS: staff UI versioned DAG, explicit choices, all four steps and route analytics')
                assert not errors, errors
                browser.close()
                print('PASS: kiosk, staff, SSE board, transfer, completion, analytics; no JS page errors')
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
