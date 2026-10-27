"""Local filesystem monitor. No discovery, remote shell, or third-party runtime."""

import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import threading
import time

LIMIT = 1440
MAX_DB = 64 * 1024 * 1024
FIELDS = ("timestamp_ms", "disk_total_bytes", "disk_free_bytes", "load1")
SCHEMA = "CREATE TABLE samples (seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms INTEGER NOT NULL, disk_total_bytes INTEGER NOT NULL, disk_free_bytes INTEGER NOT NULL, load1 REAL)"
WEB = Path(__file__).parent / "web"


def connect(data):
    path = data / "database.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("A regular initialized database is required.")
    return sqlite3.connect(path, timeout=5)


def initialize(data):
    data = Path(data)
    if data.is_symlink():
        raise ValueError("Data directory must not be a symbolic link.")
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = data / "database.sqlite3"
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(SCHEMA)
            db.execute("PRAGMA user_version=1")
    validate_database(path)


def valid_sample(row):
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        raise ValueError("Unexpected sample fields.")
    for key in FIELDS[:3]:
        if type(row[key]) is not int or not 0 <= row[key] <= 2**63 - 1:
            raise ValueError("Metric integer is outside the supported range.")
    if row["disk_free_bytes"] > row["disk_total_bytes"] or row["disk_total_bytes"] == 0:
        raise ValueError("Disk byte counts are inconsistent.")
    load = row["load1"]
    if load is not None and (
        type(load) not in (float, int) or not math.isfinite(load) or load < 0
    ):
        raise ValueError("Load must be finite and nonnegative, or absent.")
    return row


def collect(data):
    usage = shutil.disk_usage(data)
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    return valid_sample(
        dict(zip(FIELDS, (time.time_ns() // 1_000_000, usage.total, usage.free, load)))
    )


def record(data, row):
    valid_sample(row)
    with closing(connect(data)) as db, db:
        db.execute(
            "INSERT INTO samples(timestamp_ms,disk_total_bytes,disk_free_bytes,load1) VALUES (?,?,?,?)",
            tuple(row[key] for key in FIELDS),
        )
        db.execute(
            "DELETE FROM samples WHERE seq NOT IN (SELECT seq FROM samples ORDER BY seq DESC LIMIT ?)",
            (LIMIT,),
        )


def history(data):
    with closing(connect(data)) as db:
        rows = db.execute(
            "SELECT timestamp_ms,disk_total_bytes,disk_free_bytes,load1 FROM samples ORDER BY seq DESC LIMIT ?",
            (LIMIT,),
        ).fetchall()
    return [dict(zip(FIELDS, row)) for row in reversed(rows)]


def regular_bytes(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("Backup file size or type is unsupported.")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Backup file size or type is unsupported.")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Backup exceeded size bound.")
    return raw


def validate_database(path):
    if path.is_symlink():
        raise ValueError("Database must not be a symbolic link.")
    path = path.resolve()
    regular_bytes(path, MAX_DB)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",) or db.execute(
                "PRAGMA user_version"
            ).fetchone() != (1,):
                raise ValueError("Unsupported or damaged database.")
            objects = db.execute(
                "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if objects != [("samples", "table")]:
                raise ValueError("Unexpected database objects.")
            columns = db.execute("PRAGMA table_info(samples)").fetchall()
            expected = [
                (0, "seq", "INTEGER", 0, None, 1),
                (1, "timestamp_ms", "INTEGER", 1, None, 0),
                (2, "disk_total_bytes", "INTEGER", 1, None, 0),
                (3, "disk_free_bytes", "INTEGER", 1, None, 0),
                (4, "load1", "REAL", 0, None, 0),
            ]
            if columns != expected or db.execute(
                "SELECT sql FROM sqlite_master WHERE name='samples'"
            ).fetchone() != (SCHEMA,):
                raise ValueError("Unsupported database schema or constraints.")
            if db.execute(
                "SELECT count(*) FROM samples WHERE seq IS NULL OR seq <= 0"
            ).fetchone()[0]:
                raise ValueError("Invalid sample sequence.")
            count = db.execute("SELECT count(*) FROM samples").fetchone()[0]
            if count > LIMIT:
                raise ValueError("Too many samples.")
            for row in db.execute(
                "SELECT timestamp_ms,disk_total_bytes,disk_free_bytes,load1 FROM samples"
            ):
                valid_sample(dict(zip(FIELDS, row)))
            return count
    except sqlite3.Error as error:
        raise ValueError("Invalid SQLite snapshot.") from error


def backup(data, output):
    output = Path(output)
    output.mkdir(mode=0o700)
    try:
        target = output / "database.sqlite3"
        with closing(connect(data)) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
        target.chmod(0o600)
        count = validate_database(target.resolve())
        raw = regular_bytes(target, MAX_DB)
        manifest = {
            "schema_version": 1,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "rows": count,
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    except BaseException:
        shutil.rmtree(output)
        raise


def restore(source, data):
    source, data = Path(source), Path(data)
    manifest = json.loads(regular_bytes(source / "manifest.json", 4096))
    raw = regular_bytes(source / "database.sqlite3", MAX_DB)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "sha256", "bytes", "rows"}
        or manifest["schema_version"] != 1
        or manifest["bytes"] != len(raw)
        or manifest["sha256"] != hashlib.sha256(raw).hexdigest()
    ):
        raise ValueError("Snapshot manifest does not match its database.")
    # Validate a private byte copy. Re-reading a replaceable source path would race.
    data.mkdir(mode=0o700)
    try:
        db = data / "database.sqlite3"
        with db.open("xb") as stream:
            stream.write(raw)
        db.chmod(0o600)
        if validate_database(db.resolve()) != manifest["rows"]:
            raise ValueError("Snapshot row count does not match.")
    except BaseException:
        shutil.rmtree(data)
        raise


def make_server(data, port=8086, interval=10, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.send_body(405, b"Read-only service.", "text/plain")

        do_PUT = do_DELETE = do_POST

        def send_body(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in (
                f"localhost:{self.server.server_port}",
                f"127.0.0.1:{self.server.server_port}",
            ):
                self.send_body(403, b"Loopback host required.", "text/plain")
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/style.css": ("style.css", "text/css"),
                "/app.js": ("app.js", "text/javascript"),
            }
            try:
                if self.path in assets:
                    name, mime = assets[self.path]
                    self.send_body(200, (WEB / name).read_bytes(), mime)
                    return
                if self.path not in ("/api/history", "/healthz", "/metrics"):
                    self.send_body(404, b"Not found.", "text/plain")
                    return
                rows = history(data)
                latest = rows[-1] if rows else None
                age = (
                    (time.time_ns() // 1_000_000 - latest["timestamp_ms"]) / 1000
                    if latest
                    else None
                )
                healthy = age is not None and 0 <= age <= max(30, 3 * interval)
                if self.path == "/healthz":
                    self.send_body(
                        200 if healthy else 503,
                        json.dumps(
                            {
                                "status": "ok" if healthy else "stale-or-empty",
                                "sample_age_seconds": age,
                            }
                        ).encode(),
                        "application/json",
                    )
                    return
                if self.path == "/api/history":
                    payload = {
                        "schema_version": 1,
                        "scope": "data-filesystem-and-visible-os-load",
                        "retention_samples": LIMIT,
                        "interval_seconds": interval,
                        "healthy": healthy,
                        "samples": rows,
                    }
                    self.send_body(
                        200,
                        json.dumps(payload, allow_nan=False).encode(),
                        "application/json",
                    )
                    return
                lines = [
                    "# TYPE homelab_collector_healthy gauge",
                    f"homelab_collector_healthy {int(healthy)}",
                ]
                if latest:
                    for key in ("disk_total_bytes", "disk_free_bytes", "load1"):
                        if latest[key] is not None:
                            lines.extend(
                                [
                                    f"# TYPE homelab_{key} gauge",
                                    f"homelab_{key} {latest[key]}",
                                ]
                            )
                    lines.append(
                        f"homelab_sample_timestamp_seconds {latest['timestamp_ms'] / 1000}"
                    )
                self.send_body(
                    200, ("\n".join(lines) + "\n").encode(), "text/plain; version=0.0.4"
                )
            except (OSError, ValueError, sqlite3.Error):
                self.send_body(503, b"Monitor data is unavailable.", "text/plain")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8086)
    serve.add_argument("--interval", type=int, default=10)
    serve.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    snap = sub.add_parser("backup")
    snap.add_argument("--data", type=Path, required=True)
    snap.add_argument("--out", type=Path, required=True)
    res = sub.add_parser("restore")
    res.add_argument("--backup", type=Path, required=True)
    res.add_argument("--data", type=Path, required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            print(json.dumps(backup(args.data, args.out), indent=2))
            return 0
        if args.command == "restore":
            restore(args.backup, args.data)
            print("Snapshot restored into a new data directory.")
            return 0
        if args.command == "demo":
            args.out.mkdir()
            initialize(args.out / "data")
            for i, free in enumerate((800, 750, 700, 680, 720)):
                record(
                    args.out / "data",
                    dict(zip(FIELDS, (1700000000000 + i * 10000, 1000, free, None))),
                )
            receipt = backup(args.out / "data", args.out / "backup")
            restore(args.out / "backup", args.out / "restored")
            assert history(args.out / "data") == history(args.out / "restored")
            print(
                json.dumps(
                    {
                        "fixture": "synthetic",
                        "samples": history(args.out / "restored"),
                        "restore_equal": True,
                        "backup_rows": receipt["rows"],
                    },
                    indent=2,
                )
            )
            return 0
        if not 1 <= args.interval <= 3600 or not 1 <= args.port <= 65535:
            raise ValueError("Interval or port is outside the supported range.")
        initialize(args.data)
        record(args.data, collect(args.data))
        stop = threading.Event()

        def worker():
            while not stop.wait(args.interval):
                try:
                    record(args.data, collect(args.data))
                except (OSError, ValueError, sqlite3.Error):
                    print("Collection failed; retained prior samples.", file=sys.stderr)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        with make_server(args.data, args.port, args.interval, args.host) as server:
            print(
                f"Local monitor port {server.server_port}; metrics describe the data filesystem.",
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                thread.join()
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
