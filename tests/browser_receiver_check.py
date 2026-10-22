"""Headless Chrome layout/DOM check of the receiver panel; no serial or RF writes."""
import asyncio
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request

import websockets


async def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="receiver-browser-") as temp:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--headless=new", "--disable-gpu", "--no-first-run",
            "--remote-debugging-port=0", f"--user-data-dir={temp}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup)
        try:
            port_file = Path(temp)/"DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                await asyncio.sleep(.1)
            port = int(port_file.read_text().splitlines()[0])
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
            target = next(t for t in tabs if t["type"]=="page")
            async with websockets.connect(target["webSocketDebuggerUrl"],max_size=10_000_000) as ws:
                count = 0
                async def command(method, **params):
                    nonlocal count
                    count += 1
                    await ws.send(json.dumps({"id":count,"method":method,"params":params}))
                    while True:
                        response = json.loads(await asyncio.wait_for(ws.recv(),timeout=10))
                        if response.get("id")==count:
                            if "error" in response:
                                raise RuntimeError(response["error"])
                            return response.get("result",{})
                async def evaluate(expression):
                    result = await command("Runtime.evaluate",expression=expression,returnByValue=True,awaitPromise=True)
                    if "exceptionDetails" in result:
                        raise RuntimeError(result["exceptionDetails"])
                    return result["result"].get("value")
                await command("Page.enable")
                await command("Runtime.enable")
                await command("Page.navigate",url="http://127.0.0.1:8080/")
                for _ in range(100):
                    ready = await evaluate("document.readyState==='complete' && typeof renderReceiver==='function'")
                    if ready:
                        break
                    await asyncio.sleep(.1)
                assert ready
                await asyncio.sleep(1)
                # Test zero coordinates and fix loss atomically, then restore live state.
                result = await evaluate("""(() => {
                  const sample={connected:true,port:'COM13',baud:115200,status:'Fresh reported fix',fresh:true,
                    latitude:0,longitude:0,satellites:6,hdop:1.2,position_age_s:0,format:'ESP32 text',
                    checksum_errors:0,last_position:{latitude:0,longitude:0},lines:[],satellites_in_view:[]};
                  renderReceiver(sample);
                  const valid=$('receiverLat').textContent==='0.000000' && $('receiverLon').textContent==='0.000000';
                  renderReceiver({...sample,fresh:false,status:'No fresh GPS position',latitude:null,longitude:null});
                  return {valid,cleared:$('receiverLat').textContent==='--',last:$('receiverLast').textContent};
                })()""")
                assert result["valid"] and result["cleared"] and "stale" in result["last"],result
                await evaluate("fetch('/api/receiver').then(r=>r.json()).then(renderReceiver).then(()=>true)")
                for name,width,height in [("wide",1366,1100),("mobile",390,1100)]:
                    await command("Emulation.setDeviceMetricsOverride",width=width,height=height,
                                  deviceScaleFactor=1,mobile=False)
                    await evaluate("document.getElementById('receiverPanel').scrollIntoView(); new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                    layout = await evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,panel:document.getElementById('receiverPanel').getBoundingClientRect().width})")
                    assert layout["scroll"] <= layout["width"],layout
                    shot = await command("Page.captureScreenshot",format="png",captureBeyondViewport=False)
                    (root/"build"/f"receiver-{name}.png").write_bytes(base64.b64decode(shot["data"]))
                    print(name,layout)
                print("Receiver DOM, freshness and responsive layout: PASS")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            await asyncio.sleep(.5)


if __name__=="__main__":
    asyncio.run(main())
