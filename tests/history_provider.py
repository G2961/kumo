"""Deterministic model fixture. Only real OpenCode tools may change the fixtures' files."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HistoryProvider:
    def __init__(self, local_path, local_original, remote_original):
        fixture = self
        self.calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                messages = body['messages']
                last = max((i for i,m in enumerate(messages) if m['role'] == 'user'), default=0)
                content = messages[last].get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content)
                remote = 'REMOTE_ONE' in content or 'REMOTE_TWO' in content
                marker = 'REMOTE_TWO' if 'REMOTE_TWO' in content else 'REMOTE_ONE' if remote else 'LOCAL_NEW'
                read_id, write_id = 'call_history_read_' + marker, 'call_history_write_' + marker
                results = {m.get('tool_call_id') for m in messages[last + 1:] if m['role'] == 'tool'}
                available = {t['function']['name'] for t in body.get('tools', [])}
                name = None
                if write_id in results or not available:
                    delta, finish = {'content': 'HISTORY_TURN_DONE'}, 'stop'
                else:
                    writing = read_id in results
                    if remote:
                        name = 'mobile_ssh_ssh_write_file' if writing else 'mobile_ssh_ssh_read_file'
                        systems = ' '.join(str(m.get('content', '')) for m in messages if m['role'] in ['system', 'developer'])
                        connection = re.search(r'connection_id=([^;\s]+)', systems).group(1)
                        session = re.search(r'session_id=([^;\s]+)', systems).group(1)
                        args = {'connection_id': connection, 'session_id': session, 'path': '/root/test-2.txt'}
                        if writing:
                            args['content'] = remote_original + 'REMOTE_ONE\n' + ('REMOTE_TWO\n' if marker == 'REMOTE_TWO' else '')
                    else:
                        name = 'write' if writing else 'read'
                        args = {'filePath': str(local_path)}
                        if writing:
                            args['content'] = local_original.replace('LOCAL_OLD', 'LOCAL_NEW')
                    if name not in available:
                        self.send_error(500, 'Expected tool is unavailable'); return
                    fixture.calls.append(name)
                    delta = {'tool_calls': [{'index': 0, 'id': write_id if writing else read_id,
                        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
                    finish = 'tool_calls'
                def chunk(delta, reason):
                    return 'data: ' + json.dumps({'id': 'chatcmpl_history', 'object': 'chat.completion.chunk',
                        'created': 0, 'model': 'history-probe', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}) + '\n\n'
                payload = (chunk(delta, None) + chunk({}, finish) + 'data: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
