"""Local web server (stdlib only): static UI, JSON API and a Server-Sent-Events stream.

Security model - this process can put frames on a real vehicle bus, so:
  * it listens on 127.0.0.1 only
  * every API call needs the per-launch random token (embedded in the page it serves)
  * the Host header must be localhost/127.0.0.1 (blocks DNS-rebinding)
  * cross-origin browser requests are refused
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, __version__
from .app import App, AppError

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
JS_ORDER = ["core.js", "view_setup.js", "view_sim.js", "view_scen.js", "view_misc.js", "view_fw.js", "view_send.js", "view_info.js"]


class Hub:
    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        q.overflow = False      # set when an event had to be dropped: the page is then told to reload its state
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, kind, data):
        msg = (kind, json.dumps(data, separators=(",", ":")))
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    q.overflow = True

    @property
    def has_clients(self):
        return bool(self.clients)


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # a browser that closes a connection (tab closed, page reloaded) is normal - do not print a traceback for it
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Server:
    def __init__(self, workspace: str, port: int = 8765):
        self.token = secrets.token_urlsafe(24)
        self.hub = Hub()
        self.app = App(workspace, self.hub)
        self.httpd = None
        self.port = port
        self._stop = threading.Event()
        self.quit_evt = threading.Event()

    # ---- routes ---------------------------------------------------------------------------
    def post_routes(self):
        a = self.app

        def ok(**kw):
            return {"ok": True, **kw}

        r = {}
        r["/api/project/new"] = lambda b, q: ok(project=a.new_project(b.get("name", "")), state=a.state())
        r["/api/project/open"] = lambda b, q: (a.open_project(b["id"]), ok())[1]
        r["/api/project/delete"] = lambda b, q: (a.delete_project(b["id"]), ok())[1]
        r["/api/table/import"] = lambda b, q: ok(**a.import_table(b["name"], b.get("id_base", "auto"), b.get("id_format", "auto"), b.get("mapping")))
        r["/api/table/preview"] = lambda b, q: ok(**a.preview_table(b["name"], b.get("id_base", "auto"), b.get("id_format", "auto"), b.get("mapping")))
        r["/api/file/remove"] = lambda b, q: (a.remove_file(b["name"]), ok())[1]
        r["/api/roles"] = lambda b, q: (a.set_all_roles(b["map"]) if "map" in b else a.set_role(b["msg"], b["role"]),
                                        ok())[1]
        r["/api/frame/define"] = lambda b, q: ok(**a.define_frame(b))
        r["/api/frame/delete"] = lambda b, q: (a.delete_frame(b["name"]), ok())[1]
        r["/api/uds/settings"] = lambda b, q: (a.set_uds_settings(b), ok())[1]
        r["/api/uds/defaults"] = lambda b, q: (a.reset_uds_settings(), ok())[1]
        r["/api/uds/identity"] = lambda b, q: ok(**a.uds_identity())
        r["/api/uds/raw"] = lambda b, q: ok(**a.uds_raw(b["hex"], bool(b.get("expert"))))
        r["/api/uds/inspect"] = lambda b, q: ok(info=a.fw_inspect(b["name"]))
        r["/api/uds/flash"] = lambda b, q: ok(**a.start_flash(b["name"], b.get("confirm", ""), bool(b.get("recovery"))))
        r["/api/uds/abort"] = lambda b, q: (a.abort_flash(), ok())[1]
        r["/api/uds/recovery/dismiss"] = lambda b, q: (a.dismiss_recovery(), ok())[1]
        r["/api/buslog/toggle"] = lambda b, q: (a.set_buslog(bool(b["on"])), ok())[1]
        r["/api/open"] = lambda b, q: ok(path=a.open_folder(b.get("what", "")))
        r["/api/buslog/clear"] = lambda b, q: ok(removed=a.clear_buslogs())
        r["/api/manual/send"] = lambda b, q: ok(**a.manual_send(b))
        r["/api/manual/stop"] = lambda b, q: (a.manual_stop(b.get("key") or None), ok())[1]
        r["/api/manual/clear"] = lambda b, q: (a.manual_clear_finished(), ok())[1]
        r["/api/manual/save"] = lambda b, q: ok(item=a.manual_save(b))
        r["/api/manual/delete"] = lambda b, q: (a.manual_delete(b["name"]), ok())[1]
        r["/api/table/grid"] = lambda b, q: ok(**a.table_grid(b["name"]))
        r["/api/dutnode"] =lambda b, q: (a.set_dut_node(b["node"]), ok())[1]
        r["/api/composites"] =lambda b, q: (a.set_composites(b["on"]), ok())[1]
        r["/api/period/all"] = lambda b, q: ok(count=a.set_all_periods(None if b.get("reset") else b.get("ms")))
        r["/api/period"] = lambda b, q: (a.set_period(b["msg"], b["ms"]), ok())[1]
        r["/api/meta"] = lambda b, q: (a.set_meta(b), ok())[1]
        r["/api/connect"] = lambda b, q: (a.connect(b["interface"], b.get("channel", ""), b.get("bitrate", 500000)),
                                          ok())[1]
        r["/api/disconnect"] = lambda b, q: (a.disconnect(), ok())[1]
        r["/api/tx"] = lambda b, q: (a.start_tx(bool(b["on"])), ok())[1]
        r["/api/estop"] = lambda b, q: (a.estop(), ok())[1]
        r["/api/signal"] = lambda b, q: ok(**a.set_value(b["uid"], b["value"]))
        r["/api/enable"] = lambda b, q: (a.set_enabled(b["msg"], bool(b["on"])), ok())[1]
        r["/api/once"] = lambda b, q: (a.engine.send_once(b["msg"]), ok())[1]
        r["/api/auto"] = lambda b, q: (a.set_auto(b["uid"], bool(b["on"])), ok())[1]
        r["/api/baseline/reset"] = lambda b, q: (a.reset_baseline(), ok(values=a.values))[1]
        r["/api/baseline/save"] = lambda b, q: (a.save_baseline(), ok())[1]
        r["/api/scenario/save"] = lambda b, q: ok(scenario=a.save_scenario(b))
        r["/api/scenario/delete"] = lambda b, q: (a.delete_scenario(b["id"]), ok())[1]
        r["/api/scenario/restore"] = lambda b, q: (a.restore_scenarios(), ok())[1]
        r["/api/scenario/capture"] = lambda b, q: ok(scenario=a.capture_scenario(
            b.get("title", ""), b.get("expect", ""), b.get("group", ""), int(b.get("dwell_ms") or 1500)))
        r["/api/run"] = lambda b, q: (a.run(b["ids"], b.get("manual", "prompt")), ok())[1]
        r["/api/run/abort"] = lambda b, q: (a.runner.abort(), ok())[1]
        r["/api/run/verdict"] = lambda b, q: (a.runner.verdict(b["verdict"], b.get("note", "")), ok())[1]
        r["/api/result"] = lambda b, q: ok(result=a.record_manual(b))
        r["/api/result/delete"] = lambda b, q: (a.results.delete(b["ids"]), ok())[1]
        r["/api/result/clear"] = lambda b, q: (a.results.clear(), ok())[1]
        r["/api/serial/open"] = lambda b, q: (a.serial.open(b["port"], b.get("baud", 115200)), ok())[1]
        r["/api/serial/close"] = lambda b, q: (a.serial.close(), ok())[1]
        r["/api/serial/send"] = lambda b, q: (a.serial.write(b["text"], b.get("eol", "\r\n")), ok())[1]
        def quit_(b, q):
            if a.uds_run["running"]:
                raise AppError("A firmware update is running - quitting now could leave the ECU without a valid application.")
            self.quit_evt.set()
            return ok()
        r["/api/quit"] = quit_
        return r

    def get_routes(self):
        a = self.app
        r = {}
        r["/api/state"] = lambda q: a.state()
        r["/api/about"] = lambda q: a.about()
        r["/api/buslog/list"] = lambda q: a.buslog_state()
        r["/api/channels"] = lambda q: {"channels": a.channels(q.get("interface", ["pcan"])[0])}
        r["/api/serial/ports"] = lambda q: {"ports": a.serial.ports(), "available": a.serial.available,
                                            "open": a.serial.is_open}
        return r

    # ---- lifecycle ----------------------------------------------------------------------------
    def start(self):
        server = self
        posts, gets = self.post_routes(), self.get_routes()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "CANBench"

            def log_message(self, *a):
                pass

            # -- guards
            def _host_ok(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    return False
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                    return False
                return True

            def _token_ok(self, qs):
                tok = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
                return secrets.compare_digest(tok, server.token)

            def _send(self, code, body: bytes, ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj).encode("utf-8"))

            def _guard(self):
                if not self._host_ok():
                    self._json(403, {"error": "forbidden"})
                    return None
                u = urlparse(self.path)
                return u.path, parse_qs(u.query)

            def do_GET(self):
                g = self._guard()
                if g is None:
                    return
                path, qs = g
                try:
                    if path in ("/", "/index.html"):
                        html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
                        html = html.replace("__TOKEN__", server.token)
                        return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                    if path == "/static/app.js":  # one request instead of six: fewer ways to half-load
                        parts = []
                        for n in JS_ORDER:
                            parts.append(open(os.path.join(WEB, n), encoding="utf-8").read())
                        return self._send(200, "\n;\n".join(parts).encode("utf-8"), "application/javascript; charset=utf-8")
                    if path.startswith("/static/"):
                        fn = os.path.normpath(os.path.join(WEB, path[len("/static/"):]))
                        if not fn.startswith(WEB) or not os.path.isfile(fn):
                            return self._json(404, {"error": "not found"})
                        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
                        return self._send(200, open(fn, "rb").read(), ctype)
                    if path == "/api/ping":      # lets a second launch find this instance (no token, no private data)
                        return self._json(200, {"app": APP_NAME, "version": __version__, "port": server.port, "pid": os.getpid()})
                    if not self._token_ok(qs):
                        return self._json(401, {"error": "bad token"})
                    if path == "/events":
                        return self._sse()
                    if path == "/api/buslog/file":
                        fp = server.app.buslog_path(qs.get("name", [""])[0])
                        with open(fp, "rb") as fh:
                            data = fh.read()
                        return self._send(200, data, "text/plain; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{os.path.basename(fp)}"'})
                    if path == "/api/results.csv":
                        name = server.app.export_name("results", "csv")
                        return self._send(200, server.app.csv_bytes(), "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{name}"'})
                    if path == "/api/report.html":
                        name = server.app.export_name("report", "html")
                        return self._send(200, server.app.report_bytes(), "text/html; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{name}"'})
                    if path in gets:
                        return self._json(200, gets[path](qs))
                    return self._json(404, {"error": "not found"})
                except AppError as e:
                    self._json(400, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})

            def do_POST(self):
                g = self._guard()
                if g is None:
                    return
                path, qs = g
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b""
                    if not self._token_ok(qs):
                        return self._json(401, {"error": "bad token"})
                    if path == "/api/file":
                        entry = server.app.add_file(qs.get("name", [""])[0], raw)
                        return self._json(200, {"ok": True, "file": entry})
                    fn = posts.get(path)
                    if fn is None:
                        return self._json(404, {"error": "not found"})
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    return self._json(200, fn(body, qs))
                except AppError as e:
                    self._json(400, {"error": str(e)})
                except KeyError as e:
                    self._json(400, {"error": f"missing field {e}"})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})

            def _sse(self):
                q = server.hub.subscribe()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(b"retry: 1000\n\n")
                    last = time.time()
                    while not server._stop.is_set():
                        if q.overflow:
                            # the page fell behind and events were dropped (possibly the final 'done'): throw the backlog away
                            # and tell the page to fetch the current state instead of showing a stale one
                            q.overflow = False
                            try:
                                while True:
                                    q.get_nowait()
                            except queue.Empty:
                                pass
                            self.wfile.write(b"event: resync\ndata: {}\n\n")
                            self.wfile.flush()
                            last = time.time()
                        try:
                            kind, data = q.get(timeout=1.0)
                            self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            last = time.time()
                        except queue.Empty:
                            if time.time() - last > 10:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                last = time.time()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server.hub.unsubscribe(q)
                    self.close_connection = True

        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = False
        last_err = None
        for p in range(self.port, self.port + 20):
            try:
                self.httpd = QuietHTTPServer(("127.0.0.1", p), H)
                self.port = p
                break
            except OSError as e:
                last_err = e
        if self.httpd is None:
            raise RuntimeError(f"No free port: {last_err}")
        threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True).start()
        threading.Thread(target=self._broadcast, name="broadcast", daemon=True).start()

    NO_UI_TIMEOUT = 15.0  # seconds without any UI connected while transmitting -> stop TX

    def _broadcast(self):
        a = self.app
        gone_since = None
        while not self._stop.is_set():
            time.sleep(0.1)
            if not self.hub.has_clients:
                a.serial_batch()  # drop; clients re-sync via /api/state
                if a.engine.tx_on or a.engine.manual_active():
                    gone_since = gone_since or time.time()
                    if time.time() - gone_since > self.NO_UI_TIMEOUT:
                        a.runner.abort()
                        a.engine.stop_all()
                        a.serial.marker("WATCHDOG: UI closed - CAN TX stopped")
                        gone_since = None
                else:
                    gone_since = None
                continue
            gone_since = None
            try:
                self.hub.publish("live", a.live())
                rows, dropped = a.trace_batch()
                if rows or dropped:
                    self.hub.publish("trace", {"rows": rows, "dropped": dropped})
                s = a.serial_batch()
                if s:
                    self.hub.publish("serial", s)
            except Exception:
                traceback.print_exc()

    def stop(self):
        self._stop.set()
        self.app.shutdown()
        if self.httpd:
            self.httpd.shutdown()
