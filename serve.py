# Локальный сервер доски: python serve.py  (или двойной клик по start.bat)
# Нужен, потому что ES-модули не грузятся с file://, а Windows иногда отдаёт
# .js с MIME-типом text/plain — здесь MIME выставляются явно.
# Плюс: мини-API realtime-комнат (/api/room/<id>) для совместного просмотра
# и редактирования доски по ссылке. Данные комнат живут в памяти сервера.
import http.server
import json
import os
import re
import socket
import socketserver
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = 8090

# ---- realtime-комнаты: id -> {name, objects, rev, ts} ----
ROOMS = {}
ROOMS_LOCK = threading.Lock()
ROOM_RE = re.compile(r'^/api/room/([A-Za-z0-9_-]{4,40})$')
CURSORS = {}                      # id -> {who: {name,color,x,y,ts}}
CURSORS_LOCK = threading.Lock()
CURSOR_RE = re.compile(r'^/api/cursor/([A-Za-z0-9_-]{4,40})$')
CURSOR_TTL = 6
MAX_BODY = 40 * 1024 * 1024
MAX_ROOMS = 200
ROOM_TTL = 24 * 3600


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        '.js': 'text/javascript',
        '.mjs': 'text/javascript',
        '.css': 'text/css',
        '.json': 'application/json',
        '.html': 'text/html',
    }

    def log_message(self, *args):
        pass

    def end_headers(self):
        # режим разработки: без этого браузер неделю будет есть кэшированные js/css
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _send_json(self, code, obj, raw=False):
        body = obj if raw else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/info':
            return self._send_json(200, {'ok': True, 'ip': lan_ip(), 'port': PORT})
        m = ROOM_RE.match(path)
        if m:
            rid = m.group(1)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            since = 0
            try:
                since = int(qs.get('since', ['0'])[0])
            except ValueError:
                pass
            with ROOMS_LOCK:
                room = ROOMS.get(rid)
                if room:
                    room['ts'] = time.time()
                    if room['rev'] == since:
                        return self._send_json(200, {'rev': room['rev'], 'unchanged': True})
                    payload = {'rev': room['rev'], 'name': room['name'], 'objects': room['objects']}
            if not room:
                return self._send_json(404, {'error': 'no room'})
            return self._send_json(200, payload)
        m = CURSOR_RE.match(path)
        if m:
            rid = m.group(1)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            me = (qs.get('me') or [''])[0]
            now = time.time()
            with CURSORS_LOCK:
                d = CURSORS.get(rid, {})
                lst = [c for who, c in d.items() if who != me and now - c['ts'] < CURSOR_TTL]
            return self._send_json(200, {'list': lst})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        m = CURSOR_RE.match(path)
        if m:
            rid = m.group(1)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if length <= 1 or length > 64 * 1024:
                return self._send_json(400, {'error': 'bad length'})
            try:
                c = json.loads(self.rfile.read(length).decode('utf-8'))
            except Exception:
                return self._send_json(400, {'error': 'bad json'})
            who = str(c.get('from') or '')[:20]
            if not who:
                return self._send_json(400, {'error': 'no from'})
            try:
                x, y = float(c.get('x', 0)), float(c.get('y', 0))
            except (TypeError, ValueError):
                return self._send_json(400, {'error': 'bad coords'})
            entry = {'from': who, 'name': str(c.get('name') or '')[:30],
                     'color': str(c.get('color') or '#888')[:20], 'x': x, 'y': y, 'ts': time.time()}
            with CURSORS_LOCK:
                d = CURSORS.setdefault(rid, {})
                now = time.time()
                for k in [k for k, v in d.items() if now - v['ts'] > CURSOR_TTL]:
                    d.pop(k, None)
                if len(d) < 50 or who in d:
                    d[who] = entry
            return self._send_json(200, {'ok': True})
        m = ROOM_RE.match(path)
        if not m:
            return self._send_json(404, {'error': 'not found'})
        rid = m.group(1)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 1 or length > MAX_BODY:
            return self._send_json(400, {'error': 'bad length'})
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            return self._send_json(400, {'error': 'bad json'})
        if not isinstance(data, dict) or not isinstance(data.get('objects'), list) \
                or len(data['objects']) > 100000:
            return self._send_json(400, {'error': 'bad board'})
        name = str(data.get('name', 'Доска'))[:80]
        with ROOMS_LOCK:
            if len(ROOMS) >= MAX_ROOMS:  # чистка: сначала протухшие, потом древнейшие
                now = time.time()
                stale = [k for k, v in ROOMS.items() if now - v['ts'] > ROOM_TTL]
                for k in stale:
                    ROOMS.pop(k, None)
                while len(ROOMS) >= MAX_ROOMS:
                    ROOMS.pop(min(ROOMS, key=lambda k: ROOMS[k]['ts']), None)
            room = ROOMS.get(rid)
            if room:
                room['rev'] += 1
            else:
                room = {'rev': 1}
                ROOMS[rid] = room
            room.update(name=name, objects=data['objects'], ts=time.time())
            rev = room['rev']
        self._send_json(200, {'rev': rev})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True   # не падать после резкого закрытия прошлого раза
    daemon_threads = True


def already_running():
    """Наш ли сервер уже слушает порт? (узнаём по MIME js-модуля)"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/js/config.js', timeout=1.5) as r:
            return r.status == 200 and 'javascript' in r.headers.get('Content-Type', '')
    except Exception:
        return False


def main():
    url = f'http://localhost:{PORT}/'
    if already_running():
        print(f'Сервер уже запущен: {url}')
        if os.environ.get('NO_BROWSER') != '1':
            webbrowser.open(url)
        return
    try:
        # 0.0.0.0 — чтобы по ссылке могли зайти и другие устройства в сети
        httpd = Server(('0.0.0.0', PORT), Handler)
    except OSError as e:
        print(f'Не удалось занять порт {PORT}: {e}')
        print('Закройте другие копии доски и запустите снова.')
        input('Enter — выйти...')
        raise SystemExit(1)
    with httpd:
        tip = lan_ip()
        print(f'Доска открыта: {url}' + (f'   (для других устройств: http://{tip}:{PORT}/)' if tip else ''))
        print('Windows может спросить разрешение на доступ к сети — нажмите «Разрешить».')
        if os.environ.get('NO_BROWSER') != '1':
            threading.Timer(0.8, webbrowser.open, [url]).start()
        httpd.serve_forever()


if __name__ == '__main__':
    main()
