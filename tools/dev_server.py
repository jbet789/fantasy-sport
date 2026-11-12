"""Local stand-in for Supabase, for testing the app against a real Postgres database. NOT for production.

Serves the app/ folder and the three endpoints the app uses:
  POST /auth/v1/token?grant_type=password|refresh_token   (any known email, password "test")
  POST /rest/v1/rpc/<function>                            (runs the function as the signed-in user, role "authenticated")
Row-level security, grants and every league rule are the database's own, exactly as on Supabase.

Run: python3 tools/dev_server.py --db fl_app --port 8765
"""
import argparse, json, mimetypes, os, re, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP = Path(__file__).resolve().parent.parent / "app"
ENV = {**os.environ, "PGHOST": os.environ.get("PGHOST", "/tmp/pgtest"), "PGPORT": os.environ.get("PGPORT", "5499"), "PGUSER": os.environ.get("PGUSER", "postgres")}
DB = "fl_app"


def psql(sql):
    p = subprocess.run(["psql", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", DB], input=sql, text=True, capture_output=True, env=ENV)
    return p.returncode, p.stdout, p.stderr


def lit(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "'{" + ",".join("null" if x is None else str(x).replace("\\", "\\\\").replace('"', '\\"') for x in v) + "}'"
    if isinstance(v, (dict,)):
        return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def user_by_email(email):
    code, out, _ = psql(f"select id from auth.users where email = {lit(email)};")
    return out.strip() or None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        f = APP / ("index.html" if path in ("", "/") else path.lstrip("/"))
        if not f.resolve().is_relative_to(APP) or not f.is_file():
            self.send_response(404); self.end_headers(); return
        data = f.read_bytes()
        self.send_response(200)
        ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self.send_header("Content-Type", ct + ("; charset=utf-8" if ct.startswith("text/") or ct.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        # stand-in for Supabase Auth's "update user" (change password); the dev server keeps its fixed test password
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if u.path != "/auth/v1/user" or not auth.startswith("dev-"):
            return self.send_json(401, {"msg": "invalid JWT"})
        if len(body.get("password", "")) < 6:
            return self.send_json(422, {"code": 422, "error_code": "weak_password", "msg": "Password should be at least 6 characters."})
        return self.send_json(200, {"id": auth[4:]})

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if u.path == "/auth/v1/token":
            if "refresh_token" in body:
                uid = body["refresh_token"].removeprefix("dev-refresh-")
            else:
                uid = user_by_email(body.get("email", ""))
                if not uid or body.get("password") != "test":
                    return self.send_json(400, {"error": "invalid_grant", "error_description": "Invalid login credentials"})
            return self.send_json(200, {"access_token": "dev-" + uid, "refresh_token": "dev-refresh-" + uid, "expires_in": 3600})
        m = re.fullmatch(r"/rest/v1/rpc/([a-z_0-9]+)", u.path)
        if not m:
            return self.send_json(404, {"message": "not found"})
        auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if not auth.startswith("dev-") or not re.fullmatch(r"[0-9a-f-]{36}", auth[4:]):
            return self.send_json(401, {"message": "JWT required"})
        fn = m.group(1)
        code, out, err = psql(f"select pronargs, proargnames, proargtypes::oid[], prorettype::regtype from pg_proc where proname = {lit(fn)} and pronamespace = 'public'::regnamespace;")
        if not out.strip():
            return self.send_json(404, {"message": f"function {fn} not found"})
        code, out, err = psql(f"select format_type(t, null), n from pg_proc p, unnest(p.proargtypes::oid[], coalesce(p.proargnames, array[]::text[])) as u(t, n) where proname = {lit(fn)} and pronamespace = 'public'::regnamespace;")
        types = {n: t for t, n in (line.split("|") for line in out.strip().splitlines() if line)}
        args = []
        for k, v in body.items():
            if k not in types:
                return self.send_json(400, {"message": f"unknown argument {k}"})
            as_json = types[k] == "jsonb" and isinstance(v, (list, dict))
            args.append(f"{k} := " + ("'" + json.dumps(v).replace("'", "''") + "'" if as_json else lit(v)) + f"::{types[k]}")
        _, rt, _ = psql(f"select prorettype::regtype from pg_proc where proname = {lit(fn)} and pronamespace = 'public'::regnamespace;")
        call = f"{fn}({', '.join(args)})"
        select = f"select {call};" if rt.strip() == "void" else f"select to_json({call});"
        code, out, err = psql(f"begin; set local role authenticated; select set_config('request.jwt.claim.sub', {lit(auth[4:])}, true); {select} commit;")
        if code != 0:
            msg = next((l for l in err.splitlines() if l.startswith("ERROR:")), err).removeprefix("ERROR:").strip()
            return self.send_json(400, {"message": msg})
        lines = [l for l in out.splitlines() if l and l not in ("t",)]
        if rt.strip() == "void":
            return self.send_json(204, None)
        return self.send_json(200, json.loads(lines[-1]) if lines else None)


def serve(port, db):
    global DB
    DB = db
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="fl_app"); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"serving app + API on http://127.0.0.1:{a.port} (database {a.db})")
    serve(a.port, a.db).serve_forever()
