import hashlib
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import homelab as lab


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        lab.initialize(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, t=1000):
        return {
            "timestamp_ms": t,
            "disk_total_bytes": 100,
            "disk_free_bytes": 40,
            "load1": None,
        }

    def test_retention_keeps_newest_insertions_and_values(self):
        for i in range(1450):
            lab.record(self.data, self.sample(i))
        rows = lab.history(self.data)
        self.assertEqual(len(rows), 1440)
        self.assertEqual(rows[0]["timestamp_ms"], 10)
        self.assertEqual(rows[-1]["timestamp_ms"], 1449)

    def test_invalid_samples_do_not_mutate_history(self):
        for changes in [
            {"disk_free_bytes": 101},
            {"timestamp_ms": True},
            {"load1": float("nan")},
            {"disk_total_bytes": -1},
        ]:
            with self.assertRaises(ValueError):
                lab.record(self.data, {**self.sample(), **changes})
        self.assertEqual(lab.history(self.data), [])

    def test_actual_collection_obeys_units(self):
        row = lab.collect(self.data)
        self.assertGreater(row["timestamp_ms"], 0)
        self.assertGreater(row["disk_total_bytes"], 0)
        self.assertLessEqual(row["disk_free_bytes"], row["disk_total_bytes"])
        lab.record(self.data, row)
        self.assertEqual(lab.history(self.data), [row])

    def test_snapshot_restore_and_refuse_overwrite(self):
        lab.record(self.data, self.sample())
        dest = self.root / "backup"
        receipt = lab.backup(self.data, dest)
        self.assertEqual(receipt["rows"], 1)
        self.assertEqual(
            receipt["sha256"],
            hashlib.sha256((dest / "database.sqlite3").read_bytes()).hexdigest(),
        )
        restored = self.root / "restored"
        lab.restore(dest, restored)
        self.assertEqual(lab.history(restored), lab.history(self.data))
        with self.assertRaises(FileExistsError):
            lab.restore(dest, restored)
        with self.assertRaises(FileExistsError):
            lab.backup(self.data, dest)

    def test_corrupt_snapshot_rejected_without_creating_destination(self):
        dest = self.root / "backup"
        lab.backup(self.data, dest)
        with (dest / "database.sqlite3").open("ab") as stream:
            stream.write(b"tamper")
        out = self.root / "restore"
        with self.assertRaises(ValueError):
            lab.restore(dest, out)
        self.assertFalse(out.exists())

    def test_hash_valid_wrong_schema_is_rejected(self):
        import sqlite3

        dest = self.root / "backup"
        dest.mkdir()
        db = dest / "database.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("create table unrelated (x text)")
        raw = db.read_bytes()
        (dest / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "rows": 0,
                }
            )
        )
        with self.assertRaises(ValueError):
            lab.restore(dest, self.root / "restore")

    def test_initialize_rejects_database_symlink(self):
        original = self.data / "database.sqlite3"
        linked = self.root / "linked"
        linked.mkdir()
        try:
            (linked / "database.sqlite3").symlink_to(original)
        except OSError:
            self.skipTest("Symlink creation unavailable.")
        with self.assertRaises(ValueError):
            lab.initialize(linked)

    def test_hash_valid_weak_schema_is_rejected(self):
        import sqlite3

        dest = self.root / "backup"
        dest.mkdir()
        db = dest / "database.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "create table samples (seq INTEGER, timestamp_ms INTEGER, disk_total_bytes INTEGER, disk_free_bytes INTEGER, load1 REAL)"
            )
            connection.execute("pragma user_version=1")
            connection.execute("insert into samples values (1,1000,100,40,NULL)")
        raw = db.read_bytes()
        (dest / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "rows": 1,
                }
            )
        )
        output = self.root / "restored"
        with self.assertRaises(ValueError):
            lab.restore(dest, output)
        self.assertFalse(output.exists())

    def test_http_routes_health_staleness_and_host_boundary(self):
        server = lab.make_server(self.data, port=0, interval=10)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, method="GET", host=None):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            conn.request(method, path, headers={} if host is None else {"Host": host})
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            conn.close()
            return status, raw

        try:
            self.assertEqual(request("/healthz")[0], 503)
            lab.record(self.data, self.sample(1))
            self.assertEqual(request("/healthz")[0], 503)
            lab.record(self.data, lab.collect(self.data))
            self.assertEqual(request("/healthz")[0], 200)
            self.assertEqual(len(json.loads(request("/api/history")[1])["samples"]), 2)
            self.assertIn(b"homelab_disk_free_bytes", request("/metrics")[1])
            self.assertEqual(request("/api/history", "HEAD"), (200, b""))
            self.assertEqual(request("/api/history", host="evil.test")[0], 403)
            self.assertEqual(request("/api/history", "POST")[0], 405)
            for path in [
                "/../homelab.py",
                "/database.sqlite3",
                "/api/history?path=/etc/passwd",
            ]:
                self.assertEqual(request(path)[0], 404)
            self.assertEqual(request("/")[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
