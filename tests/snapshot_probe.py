"""Compare real OpenCode snapshots for isolated Git and non-Git projects."""
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT.parent / '.local' / ('snapshot-probe-' + uuid4().hex[:8])
TASK.mkdir()
SERVER = 'http://127.0.0.1:58074'


def api(path, directory, body=None):
    url = SERVER + path + '?' + urllib.parse.urlencode({'directory': str(directory)})
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def run():
    evidence = {'serverVersion': api('/global/health', TASK)['version'], 'cases': []}
    for use_git in [False, True]:
        directory = TASK / ('with-git' if use_git else 'without-git')
        directory.mkdir()
        target = directory / 'proof.txt'
        target.write_text('UNCHANGED_TOP\nREPLACE_THIS\nUNCHANGED_BOTTOM\n', encoding='utf-8')
        if use_git:
            for args in [['init', '-q'], ['config', '--local', 'user.name', 'Snapshot probe'],
                         ['config', '--local', 'user.email', 'probe@example.invalid'],
                         ['add', '--', 'proof.txt'], ['commit', '-qm', 'Seed probe']]:
                subprocess.run(['git', '-C', str(directory), *args], check=True, capture_output=True)
        project = api('/project/current', directory)
        session = api('/session', directory, {'title': 'Snapshot probe', 'permission': [
            {'permission': 'edit', 'pattern': '*', 'action': 'allow'}]})
        sid = session['id']
        api('/session/' + sid + '/prompt_async', directory, {
            'model': {'providerID': 'AgentRouter-gpt-responses-gateway', 'modelID': 'gpt-5.6-sol'},
            'parts': [{'type': 'text', 'text': 'Use the edit tool to replace REPLACE_THIS with AGENT_CHANGED in proof.txt. '
                      'Keep every other line exactly as it is. Do not create any other file. Reply PROBE_DONE.'}]})
        messages = []
        for _ in range(240):
            messages = api('/session/' + sid + '/message', directory)
            statuses = api('/session/status', directory)
            state = statuses.get(sid, {'type': 'idle'})
            if state['type'] == 'retry':
                indicators = [term for term in ['429', 'rate limit', 'ratelimit']
                              if term in str(state.get('message', '')).lower()]
                if indicators:
                    api('/session/' + sid + '/abort', directory, {})
                    raise RuntimeError('Provider rate limit')
            if any(m['info']['role'] == 'assistant' and m['info'].get('time', {}).get('completed') for m in messages) and state['type'] == 'idle':
                break
            time.sleep(.5)
        assert 'AGENT_CHANGED' in target.read_text('utf-8'), 'Real edit did not happen'
        time.sleep(1)
        diff = api('/session/' + sid + '/diff', directory)
        parts = [p for m in messages for p in m.get('parts', [])]
        tools = [p for p in parts if p['type'] == 'tool']
        case = {'gitProject': use_git, 'projectVcs': project.get('vcs'), 'fileChanged': True,
                'sessionDiffFiles': len(diff),
                'stepSnapshotPresent': any(p['type'] == 'step-start' and p.get('snapshot') for p in parts),
                'patchParts': sum(p['type'] == 'patch' for p in parts),
                'toolMetadata': [{'name': p['tool'], 'status': p['state']['status'],
                                  'keys': sorted(p['state'].get('metadata', {}))} for p in tools]}
        evidence['cases'].append(case)
        print(json.dumps(case), flush=True)
    (TASK / 'evidence.json').write_text(json.dumps(evidence, indent=2), 'utf-8')
    (ROOT.parent / '.local/snapshot-probe-last.txt').write_text(str(TASK), 'utf-8')
    return evidence


if __name__ == '__main__':
    run()
