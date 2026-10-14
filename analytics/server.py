"""Small first-party counters. Bind to loopback; Caddy protects the dashboard."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_TIME = timezone(timedelta(hours=8))
ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
FIELDS = {'event_id', 'visitor_id', 'kind', 'count'}
DRAW_BUCKETS = ((1, 1, '1 次'), (2, 5, '2–5 次'), (6, 10, '6–10 次'),
                (11, 30, '11–30 次'), (31, 100, '31–100 次'), (101, None, '101 次及以上'))


def validate_event(event):
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise ValueError('invalid_fields')
    if any(not isinstance(event[k], str) or not ID_PATTERN.fullmatch(event[k])
           for k in ('event_id', 'visitor_id')):
        raise ValueError('invalid_identifier')
    if event['kind'] not in ('visit', 'draw', 'share'):
        raise ValueError('invalid_kind')
    if type(event['count']) is not int:
        raise ValueError('invalid_count')
    allowed = (1, 10) if event['kind'] == 'draw' else (1,)
    if event['count'] not in allowed:
        raise ValueError('invalid_count')
    return event


class EventConflict(Exception):
    pass


class Store:
    def __init__(self, path, secret):
        self.path = str(path)
        self.secret = secret.encode()
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    visitor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    day TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_day_kind ON events(day, kind);
                CREATE INDEX IF NOT EXISTS events_visitor_kind ON events(visitor, kind);
            ''')
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def digest(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def record(self, event, now=None):
        validate_event(event)
        now = now or datetime.now(timezone.utc)
        visitor = self.digest(event['visitor_id'])
        received = now.astimezone(timezone.utc).isoformat()
        day = now.astimezone(LOCAL_TIME).date().isoformat()
        with closing(self.connect()) as db:
            with db:
                cursor = db.execute('''INSERT OR IGNORE INTO events
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (event['event_id'], visitor, event['kind'], event['count'], received, day))
                if cursor.rowcount:
                    return True
                prior = db.execute('SELECT visitor,kind,units FROM events WHERE event_id=?',
                                   (event['event_id'],)).fetchone()
                if tuple(prior) != (visitor, event['kind'], event['count']):
                    raise EventConflict('event_id_conflict')
                return False

    def summary(self, now=None):
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(LOCAL_TIME).date().isoformat()
        since = (now.astimezone(LOCAL_TIME).date() - timedelta(days=29)).isoformat()
        columns = '''COUNT(DISTINCT visitor) AS visitors,
            COUNT(DISTINCT CASE WHEN kind='draw' THEN visitor END) AS players,
            COALESCE(SUM(CASE WHEN kind='visit' THEN units ELSE 0 END),0) AS pageviews,
            COALESCE(SUM(CASE WHEN kind='draw' THEN units ELSE 0 END),0) AS draws,
            COALESCE(SUM(CASE WHEN kind='share' THEN units ELSE 0 END),0) AS shares'''
        distribution = [{'min': lower, 'max': upper, 'label': label, 'players': 0}
                        for lower, upper, label in DRAW_BUCKETS]
        bucket_case = 'CASE ' + ' '.join(
            f'WHEN draws<={upper} THEN {index}'
            for index, (_, upper, _) in enumerate(DRAW_BUCKETS) if upper is not None
        ) + f' ELSE {len(DRAW_BUCKETS) - 1} END'
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            total = dict(db.execute('SELECT ' + columns + ' FROM events').fetchone())
            daily = [dict(row) for row in db.execute(
                'SELECT day,' + columns + ' FROM events WHERE day>=? GROUP BY day ORDER BY day DESC', (since,))]
            dates = db.execute('SELECT MIN(received_at),MAX(received_at) FROM events').fetchone()
            for row in db.execute(f'''SELECT {bucket_case} AS bucket, COUNT(*) AS players
                    FROM (SELECT SUM(units) AS draws FROM events WHERE kind='draw' GROUP BY visitor)
                    GROUP BY bucket'''):
                distribution[row['bucket']]['players'] = row['players']
            db.commit()
        today_counts = next((row for row in daily if row['day'] == today),
                            {'day': today, 'visitors': 0, 'players': 0, 'pageviews': 0, 'draws': 0, 'shares': 0})
        return {'totals': total, 'today': today_counts, 'daily': daily, 'timezone': 'UTC+8',
                'day': today, 'first_event_at': dates[0], 'last_event_at': dates[1],
                'draw_distribution': distribution}


class Limits:
    """Short-lived rate-limit keys only; no IP address is retained on disk."""
    def __init__(self, visitor_limit=240, network_limit=1800):
        self.visitor_limit = visitor_limit
        self.network_limit = network_limit
        self.window = None
        self.counts = {}
        self.lock = threading.Lock()

    def allow(self, visitor, network):
        window = int(time.monotonic() // 60)
        with self.lock:
            if window != self.window:
                self.window = window
                self.counts.clear()
            keys = [('v:' + visitor, self.visitor_limit), ('n:' + network, self.network_limit)]
            if len(self.counts) >= 20000 or any(self.counts.get(k, 0) >= cap for k, cap in keys):
                return False
            for key, _ in keys:
                self.counts[key] = self.counts.get(key, 0) + 1
            return True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, store, origin, dashboard, limits=None):
        self.store = store
        self.origin = origin
        self.dashboard = Path(dashboard)
        self.limits = limits or Limits()
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'GameStats'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        # Deliberately omit access logs (addresses, headers and request bodies).
        pass

    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if status == 429:
            self.send_header('Retry-After', '60')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path).path
        try:
            if route == '/':
                self.reply(200, self.server.dashboard.read_bytes(), 'text/html; charset=utf-8')
            elif route == '/summary':
                self.reply(200, self.server.store.summary())
            elif route == '/health':
                with closing(self.server.store.connect()) as db:
                    db.execute('SELECT 1 FROM events LIMIT 1').fetchone()
                self.reply(200, {'ok': True})
            else:
                self.reply(405 if route == '/event' else 404, {'error': 'not_available'})
        except (OSError, sqlite3.Error) as error:
            print(json.dumps({'level': 'error', 'operation': 'read', 'error': type(error).__name__}), flush=True)
            self.reply(503, {'error': 'temporarily_unavailable'})

    def do_POST(self):
        if urlsplit(self.path).path != '/event':
            self.reply(404, {'error': 'not_found'})
            return
        if self.headers.get('Origin') != self.server.origin:
            self.reply(403, {'error': 'origin_not_allowed'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.reply(415, {'error': 'json_required'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                self.reply(413, {'error': 'invalid_body_size'})
                return
            event = validate_event(json.loads(self.rfile.read(length)))
            visitor = self.server.store.digest(event['visitor_id'])
            # Caddy supplies the client address. Only a temporary HMAC is used.
            network = self.server.store.digest(self.headers.get('X-Forwarded-For', self.client_address[0]))
            if not self.server.limits.allow(visitor, network):
                self.reply(429, {'error': 'rate_limited'})
                return
            recorded = self.server.store.record(event)
            self.reply(200, {'recorded': recorded})
        except (ValueError, TypeError, UnicodeError):
            self.reply(400, {'error': 'invalid_event'})
        except EventConflict:
            self.reply(409, {'error': 'event_id_conflict'})
        except TimeoutError:
            self.reply(408, {'error': 'request_timeout'})
        except sqlite3.Error as error:
            print(json.dumps({'level': 'error', 'operation': 'record', 'error': type(error).__name__}), flush=True)
            self.reply(503, {'error': 'temporarily_unavailable'})

    def do_OPTIONS(self):
        self.reply(403, {'error': 'cross_origin_not_allowed'})


if __name__ == '__main__':
    secret = os.environ['STATS_HASH_KEY']
    origin = os.environ['STATS_ORIGIN']
    database = os.environ['STATS_DATABASE']
    port = int(os.environ.get('STATS_PORT', '8765'))
    store = Store(database, secret)
    server = Server(('127.0.0.1', port), store, origin, Path(__file__).with_name('dashboard.html'))
    print(json.dumps({'level': 'info', 'message': 'statistics service ready', 'port': port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
