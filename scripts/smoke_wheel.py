#!/usr/bin/env python3
"""Launch an installed wheel outside the checkout using a disposable home directory."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',type=Path,required=True)
    args=parser.parse_args();python=str(args.python.absolute())
    with tempfile.TemporaryDirectory(prefix='sub2easy-wheel-') as directory:
        root=Path(directory)
        env={**os.environ,'HOME':str(root),'XDG_DATA_HOME':str(root/'xdg')}
        env.pop('PYTHONPATH',None)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        data=(root/'Library'/'Application Support'/'Sub2Easy') if sys.platform=='darwin' else root/'xdg'/'sub2easy'
        opener=build_opener(ProxyHandler({}))
        with (root/'server.log').open('w+') as log:
            process=subprocess.Popen([python,'-m','sub2easy.gui','--no-browser','--port',str(port)],
                                     cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None:raise RuntimeError('Installed application exited during startup')
                    if (data/'launch-url.txt').exists():
                        try:
                            with opener.open(f'http://127.0.0.1:{port}/',timeout=1) as response:
                                if response.status==200:break
                        except URLError:pass
                    time.sleep(.1)
                else:raise RuntimeError('Installed application failed readiness check')
                token=parse_qs(urlsplit((data/'launch-url.txt').read_text()).fragment)['token'][0]
                def call(path,body=None,authenticated=True):
                    headers={'Content-Type':'application/json'}
                    if authenticated:headers['x-local-token']=token
                    request=Request(f'http://127.0.0.1:{port}'+path,headers=headers,
                                    data=None if body is None else json.dumps(body).encode())
                    with opener.open(request,timeout=5) as response:return json.load(response)
                try:call('/api/status',authenticated=False)
                except HTTPError as e:assert e.code==401
                else:raise AssertionError('Unauthenticated API was accepted')
                status=call('/api/status');assert status['initialized'] is False
                call('/api/unlock',{'setup':True,'password':'synthetic-smoke-master-password'})
                state=call('/api/state')
                assert state['accounts']==[] and state['jobs']==[]
                assert state['monitor']['config']['enabled'] is False
                assert state['retirement']['config']['enabled'] is False
                assert not state['settings']['has_admin_key'] and not state['settings']['has_cookie']
                call('/api/lock',{})
                assert call('/api/status')['unlocked'] is False
                print('PASS: installed wheel, isolated user data, HTTP ready, API auth, lock/unlock, empty safe defaults')
            finally:
                process.terminate()
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait();raise RuntimeError('Installed server did not stop cleanly')


if __name__=='__main__':main()
