"""Shared helper: DCP connection to the running app WebView (transport autodetect)."""
import json
import subprocess
import urllib.request

SERIAL_PREFS = ['adb-24301FDF600063-', '192.168.1.147:']

def adb_serial():
    out = subprocess.check_output(['adb', 'devices'], text=True)
    for line in out.splitlines()[1:]:
        name = line.split('\t')[0].strip()
        if line.strip().endswith('device') and name:
            for pref in SERIAL_PREFS:
                if name.startswith(pref):
                    return name
            return name
    raise RuntimeError('no adb device')

ADB = None

def adb(args, **kw):
    global ADB
    ADB = ADB or adb_serial()
    if isinstance(args, str):
        args = args.split()
    return subprocess.run(['adb', '-s', ADB] + args, text=True, capture_output=True, **kw)

def connect_dcp():
    global ADB
    ADB = ADB or adb_serial()
    r = adb("shell cat /proc/net/unix | grep -ao 'webview_devtools_remote_[0-9]*' | tail -1")
    sock = r.stdout.strip()
    if not sock:
        raise RuntimeError('no webview devtools socket (app dead?)')
    adb(['forward', '--remove', 'tcp:9333'])
    adb(['forward', 'tcp:9333', 'localabstract:' + sock])
    page = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]
    ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=90, suppress_origin=True)
    mid = [0]

    def ev(expr, await_promise=True):
        mid[0] += 1
        ws.send(json.dumps({'id': mid[0], 'method': 'Runtime.evaluate',
                            'params': {'expression': expr, 'returnByValue': True, 'awaitPromise': await_promise}}))
        while True:
            m = json.loads(ws.recv())
            if m.get('id') == mid[0]:
                r = m.get('result', {}).get('result', {})
                return r.get('value', r.get('description', r))

    return ev, ws

try:
    import websocket  # noqa
except ImportError:
    raise
