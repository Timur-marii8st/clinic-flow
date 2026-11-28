#!/usr/bin/env python3
"""Dedicated Linux browser supervisor; no root, debugging port or TLS bypass."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


def prepare_url(raw,autoplay=False):
    u=urlsplit(raw)
    if (u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.fragment
            or u.path not in ('/board','/kiosk')):
        raise ValueError('Set a board or kiosk URL without credentials')
    if u.scheme=='http' and u.hostname not in ('localhost','127.0.0.1','::1'):
        raise ValueError('Plain HTTP is allowed only for a local demo')
    _=u.port
    pairs=parse_qsl(u.query,keep_blank_values=True)
    if any(k not in ('room','device','audio') for k,v in pairs):
        raise ValueError('Unexpected URL parameter')
    params={k:v for k,v in pairs if k=='room'}
    if u.path=='/kiosk' and 'room' in params:
        raise ValueError('Room filter is board-only')
    if 'room' in params and (not 1<=len(params['room'])<=16 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in params['room'])):
        raise ValueError('Invalid room filter')
    params['device']='1'
    if autoplay and u.path=='/board':
        params['audio']='1'
    return urlunsplit((u.scheme,u.netloc,u.path,urlencode(params),'')),urlunsplit((u.scheme,u.netloc,'/health','',''))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def healthy(url):
    try:
        # System CA verification remains enabled.
        with build_opener(NoRedirect).open(Request(url,headers={'Accept':'application/json'}),timeout=3) as response:
            return response.status==200 and json.loads(response.read(4096)).get('status')=='ok'
    except (OSError,ValueError,AttributeError):
        return False


class Watchdog:
    """Restart on verified recovery after two failed checks; not renderer-hang detection."""
    def __init__(self):
        self.failures=0

    def update(self,ok,alive):
        if not ok:
            self.failures=min(2,self.failures+1)
            return 'wait'
        recovered=self.failures>=2
        self.failures=0
        if not alive:
            return 'start'
        return 'restart' if recovered else 'wait'


def stop_browser(process):
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    autoplay=os.environ.get('CF_AUTOPLAY','0')=='1'
    url,health=prepare_url(os.environ.get('CF_KIOSK_URL',''),autoplay)
    browser=os.environ.get('CF_CHROMIUM') or shutil.which('chromium') or shutil.which('chromium-browser')
    if not browser or not Path(browser).is_file():
        raise RuntimeError('Install Chromium or set CF_CHROMIUM to its absolute executable path')
    profile=Path(os.environ.get('CF_PROFILE',str(Path.home()/'.local/share/clinic-flow/chromium'))).expanduser()
    profile.mkdir(parents=True,exist_ok=True,mode=0o700)
    profile.chmod(0o700)
    args=[browser,'--kiosk','--no-first-run','--no-default-browser-check','--disable-session-crashed-bubble',
          '--disable-background-mode','--user-data-dir='+str(profile)]
    if autoplay:
        # Explicit opt-in for a dedicated browser profile, not a general workstation.
        args.append('--autoplay-policy=no-user-gesture-required')
    args.append(url)
    stopping=False
    def terminate(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGINT,terminate)
    watchdog=Watchdog()
    process=None
    try:
        while not stopping:
            ok=healthy(health)
            action=watchdog.update(ok,process is not None and process.poll() is None)
            if stopping:
                break
            if action in ('start','restart'):
                stop_browser(process)
                print('Starting dedicated kiosk browser' if action=='start' else 'Network recovered; reopening kiosk',flush=True)
                process=subprocess.Popen(args,stdin=subprocess.DEVNULL)
            # Sleep in small slices so systemd shutdown is prompt.
            for _ in range(10):
                if stopping:break
                time.sleep(0.5)
    finally:
        stop_browser(process)


if __name__=='__main__':
    main()
