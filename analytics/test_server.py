import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from server import EventConflict, Limits, Server, Store


def event(number, visitor=1, kind='draw', count=1):
    return {'event_id': f'{number:032x}', 'visitor_id': f'{visitor:032x}', 'kind': kind, 'count': count}


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='.test-', dir=Path(__file__).resolve().parent)
        self.path = Path(self.folder.name) / 'events.sqlite3'
        self.store = Store(self.path, 'test-only-key')

    def tearDown(self):
        self.folder.cleanup()

    def test_counts_deduplication_and_restart(self):
        self.store.record(event(1, kind='visit'))
        self.store.record(event(2, kind='visit'))
        self.store.record(event(3))
        self.store.record(event(4, count=10))
        self.store.record(event(5, kind='share'))
        self.store.record(event(6, visitor=2))
        self.assertFalse(self.store.record(event(4, count=10)))
        totals = Store(self.path, 'test-only-key').summary()['totals']
        self.assertEqual(totals, {'visitors': 2, 'players': 2, 'pageviews': 2, 'draws': 12, 'shares': 1})
        with self.assertRaises(EventConflict):
            self.store.record(event(4, count=1))

    def test_midnight_uses_singapore_time_and_unique_is_not_daily_sum(self):
        self.store.record(event(1), datetime(2026, 10, 13, 15, 59, tzinfo=timezone.utc))
        self.store.record(event(2), datetime(2026, 10, 13, 16, 1, tzinfo=timezone.utc))
        summary = self.store.summary(datetime(2026, 10, 13, 16, 2, tzinfo=timezone.utc))
        self.assertEqual(summary['today']['day'], '2026-10-14')
        self.assertEqual(summary['today']['draws'], 1)
        self.assertEqual(summary['totals']['players'], 1)
        self.assertEqual(sum(day['players'] for day in summary['daily']), 2)

    def test_concurrent_retries_record_only_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            recorded = list(pool.map(lambda _: self.store.record(event(8, count=10)), range(16)))
        self.assertEqual(sum(recorded), 1)
        self.assertEqual(self.store.summary()['totals']['draws'], 10)

    def test_draw_distribution_empty_and_nonplayers(self):
        self.store.record(event(1, kind='visit'))
        self.store.record(event(2, visitor=2, kind='share'))
        distribution = self.store.summary()['draw_distribution']
        self.assertEqual([(bucket['min'], bucket['max']) for bucket in distribution],
                         [(1, 1), (2, 5), (6, 10), (11, 30), (31, 100), (101, None)])
        self.assertEqual([bucket['players'] for bucket in distribution], [0] * 6)

    def test_draw_distribution_boundaries_lifetime_and_retries(self):
        totals = [1, 2, 5, 6, 10, 11, 30, 31, 100, 101, 211]
        older = datetime(2026, 9, 1, tzinfo=timezone.utc)
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        serial = 0
        for visitor, draws in enumerate(totals, start=1):
            tens, singles = divmod(draws, 10)
            for units in [10] * tens + [1] * singles:
                serial += 1
                last = event(serial, visitor=visitor, count=units)
                self.store.record(last, older if units == 10 else now)
        self.assertFalse(self.store.record(last, now))
        self.store.record(event(serial + 1, visitor=99, kind='visit'), now)
        summary = Store(self.path, 'test-only-key').summary(now)
        distribution = summary['draw_distribution']
        self.assertEqual([bucket['players'] for bucket in distribution], [1, 2, 2, 2, 2, 2])
        self.assertEqual(sum(bucket['players'] for bucket in distribution), summary['totals']['players'])
        self.assertEqual(summary['totals']['draws'], sum(totals))

    def test_http_rejection_rate_limit_and_valid_flow(self):
        server = Server(('127.0.0.1', 0), self.store, 'https://game.example',
                        Path(__file__).with_name('dashboard.html'), Limits(visitor_limit=2))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(payload, origin='https://game.example', raw=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            body = raw if raw is not None else json.dumps(payload)
            connection.request('POST', '/event', body, {'Origin': origin, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            status = response.status
            response.read(); connection.close()
            return status
        try:
            self.assertEqual(request(event(1), origin='https://foreign.example'), 403)
            self.assertEqual(request(event(1, count=999)), 400)
            self.assertEqual(request(event(1), raw='x'*3000), 413)
            self.assertEqual(request(event(1, count=True)), 400)
            self.assertEqual(self.store.summary()['totals']['draws'], 0)
            self.assertEqual(request(event(1, count=10)), 200)
            self.assertEqual(request(event(1, count=10)), 200)
            self.assertEqual(request(event(2)), 429)
            self.assertEqual(self.store.summary()['totals']['draws'], 10)
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
