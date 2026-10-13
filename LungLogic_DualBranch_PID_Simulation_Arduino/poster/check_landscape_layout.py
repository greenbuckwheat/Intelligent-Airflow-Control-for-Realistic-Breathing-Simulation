"""Inspect rendered poster bounds through Chrome DevTools."""
import json, subprocess, tempfile, time, urllib.request, sys
from pathlib import Path
import websocket

HERE=Path(__file__).resolve().parent
html=HERE/(sys.argv[1] if len(sys.argv)>1 else 'LungLogic_Poster_2026-10-07.html')
with tempfile.TemporaryDirectory(prefix='lunglogic_check_') as profile:
    proc=subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless','--disable-gpu','--no-first-run','--remote-debugging-port=9338','--remote-allow-origins=*',f'--user-data-dir={profile}',html.as_uri()],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(40):
            try:
                tabs=json.load(urllib.request.urlopen('http://localhost:9338/json'))
                tab=next(t for t in tabs if t['type']=='page')
                break
            except Exception: time.sleep(.2)
        ws=websocket.create_connection(tab['webSocketDebuggerUrl'])
        expression="""JSON.stringify([...document.querySelectorAll('.poster > *')].map((e,i)=>({i,text:(e.innerText||e.alt||'').slice(0,90),overflowX:e.scrollWidth-e.clientWidth,overflowY:e.scrollHeight-e.clientHeight,top:parseFloat(e.style.top),height:parseFloat(e.style.height),width:parseFloat(e.style.width)})).filter(e=>e.overflowX>2||e.overflowY>2))"""
        ws.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True}}))
        while True:
            result=json.loads(ws.recv())
            if result.get('id')==1: break
        print(result['result']['result']['value'])
        ws.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        # Chrome child processes can hold the temporary profile briefly.
        time.sleep(.5)
