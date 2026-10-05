import base64
import hashlib
import io
import ipaddress
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Flask, jsonify, request, send_file

APP_DIR = Path(__file__).resolve().parent
PAYLOAD_PATH = APP_DIR / "payload.json"
PAYLOAD_BASE_URL = "https://raw.githubusercontent.com/Baatiku/candlefollowOrigin/omnilive-build-bridge/omnilive-build-bridge/"
ARTIFACT_PATH = Path("/tmp/omnilive-debug.apk")

CONTROL_TOKEN = os.environ["BRIDGE_TOKEN"]
ARTIFACT_SHARE_TOKEN = os.environ.get("ARTIFACT_SHARE_TOKEN", "")
PAYLOAD_KEY = bytes.fromhex(os.environ["PAYLOAD_KEY_HEX"])
SSH_SEED = bytes.fromhex(os.environ["SSH_SEED_HEX"])
if len(PAYLOAD_KEY) != 16:
    raise RuntimeError("PAYLOAD_KEY_HEX must decode to 16 bytes")
if len(SSH_SEED) != 32:
    raise RuntimeError("SSH_SEED_HEX must decode to 32 bytes")

from cryptography.hazmat.primitives.asymmetric import ed25519
_ssh_private = ed25519.Ed25519PrivateKey.from_private_bytes(SSH_SEED)
_ssh_pem = _ssh_private.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.OpenSSH,
    serialization.NoEncryption(),
)
SSH_KEY = paramiko.Ed25519Key.from_private_key(io.StringIO(_ssh_pem.decode()))
SSH_PUBLIC_TEXT = f"{SSH_KEY.get_name()} {SSH_KEY.get_base64()} omnilive-render-bridge"
print(f"OMNILIVE_SSH_PUBLIC_KEY={SSH_PUBLIC_TEXT}", flush=True)

app = Flask(__name__)
_lock = threading.Lock()
_state = {
    "status": "idle",
    "message": "ready; stable bridge credentials loaded",
    "started_at": None,
    "finished_at": None,
    "host": None,
    "sha256": None,
    "log": "",
}

def _auth():
    supplied = request.args.get("token", "")
    return secrets.compare_digest(supplied, CONTROL_TOKEN)

def _append_log(text):
    print(text, end="", flush=True)
    with _lock:
        current = (_state.get("log") or "") + text
        _state["log"] = current[-20000:]

def _xtea_block(v0, v1, key_words):
    delta = 0x9E3779B9
    total = 0
    mask = 0xFFFFFFFF
    for _ in range(32):
        mix = ((((v1 << 4) & mask) ^ (v1 >> 5)) + v1) & mask
        v0 = (v0 + (mix ^ ((total + key_words[total & 3]) & mask))) & mask
        total = (total + delta) & mask
        mix = ((((v0 << 4) & mask) ^ (v0 >> 5)) + v0) & mask
        v1 = (v1 + (mix ^ ((total + key_words[(total >> 11) & 3]) & mask))) & mask
    return v0, v1

def _decrypt(ciphertext, file_index):
    if PAYLOAD_KEY is None or len(PAYLOAD_KEY) != 16:
        raise RuntimeError("payload key is not activated")
    key_words = [int.from_bytes(PAYLOAD_KEY[i:i+4], "big") for i in range(0, 16, 4)]
    out = bytearray(len(ciphertext))
    for block_index in range((len(ciphertext) + 7) // 8):
        v0, v1 = _xtea_block(file_index & 0xFFFFFFFF, block_index & 0xFFFFFFFF, key_words)
        stream = v0.to_bytes(4, "big") + v1.to_bytes(4, "big")
        start = block_index * 8
        chunk = ciphertext[start:start+8]
        for j, b in enumerate(chunk):
            out[start+j] = b ^ stream[j]
    return bytes(out)

def _git_blob_sha(data):
    prefix = f"blob {len(data)}\0".encode()
    return hashlib.sha1(prefix + data).hexdigest()

def _fetch_payload_text(rel_path):
    url = f"{PAYLOAD_BASE_URL}{rel_path}?t={time.time_ns()}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def _restore_payload():
    payload = json.loads(_fetch_payload_text("payload.json"))
    root = Path(tempfile.mkdtemp(prefix="omnilive-src-"))
    for item in payload["files"]:
        path = item["path"]
        idx = int(item["index"])
        if "ciphertext_hex" in item:
            encrypted = bytes.fromhex(item["ciphertext_hex"])
        else:
            encrypted = bytes.fromhex(_fetch_payload_text(item["ciphertext_file"]).strip())
        plain = _decrypt(encrypted, idx)
        actual = _git_blob_sha(plain)
        if actual != item["git_sha1"]:
            raise RuntimeError(f"payload integrity failure for {path}")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plain)
    return root

def _run(client, command, timeout=3600):
    _append_log(f"\n$ {command}\n")
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=True)
    channel = stdout.channel
    chunks = []
    while not channel.exit_status_ready():
        if channel.recv_ready():
            data = channel.recv(65536).decode(errors="replace")
            chunks.append(data)
            _append_log(data)
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(65536).decode(errors="replace")
            chunks.append(data)
            _append_log(data)
        time.sleep(0.25)
    while channel.recv_ready():
        data = channel.recv(65536).decode(errors="replace")
        chunks.append(data)
        _append_log(data)
    while channel.recv_stderr_ready():
        data = channel.recv_stderr(65536).decode(errors="replace")
        chunks.append(data)
        _append_log(data)
    status = channel.recv_exit_status()
    output = "".join(chunks)
    if status != 0:
        raise RuntimeError(f"remote command failed ({status}): {command}\n{output[-5000:]}")
    return output

def _mkdirs(sftp, remote_dir):
    parts = []
    current = remote_dir
    while current not in ("", "/"):
        parts.append(current)
        current = current.rsplit("/", 1)[0] or "/"
    for path in reversed(parts):
        try:
            sftp.stat(path)
        except IOError:
            sftp.mkdir(path)

def _upload_tree(sftp, local_root, remote_root):
    for path in local_root.rglob("*"):
        rel = path.relative_to(local_root).as_posix()
        remote = f"{remote_root}/{rel}"
        if path.is_dir():
            _mkdirs(sftp, remote)
        else:
            _mkdirs(sftp, remote.rsplit("/", 1)[0])
            sftp.put(str(path), remote)

def _build(host):
    source = None
    client = None
    try:
        with _lock:
            _state.update(status="restoring", message="decrypting private build payload", log="")
        source = _restore_payload()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username="root",
            pkey=SSH_KEY,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )

        with _lock:
            _state.update(status="uploading", message="uploading restored source to DigitalOcean")

        remote_root = "/root/omnilive-build"
        _run(client, f"rm -rf {remote_root} && mkdir -p {remote_root}", timeout=120)
        sftp = client.open_sftp()
        try:
            _upload_tree(sftp, source, remote_root)
        finally:
            sftp.close()

        with _lock:
            _state.update(status="building", message="running Android tests and APK build on DigitalOcean")

        _run(client, "command -v docker >/dev/null 2>&1 || (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io)", timeout=900)
        _run(client, "systemctl start docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true", timeout=120)
        _run(client, f"cd {remote_root} && docker build -f ops/apk-builder.Dockerfile -t omnilive-apk .", timeout=3600)
        _run(client, "docker rm -f omnilive-extract >/dev/null 2>&1 || true; docker create --name omnilive-extract omnilive-apk >/dev/null", timeout=120)
        _run(client, "docker cp omnilive-extract:/srv/omnilive-debug.apk /root/omnilive-debug.apk && docker rm -f omnilive-extract >/dev/null", timeout=120)

        sftp = client.open_sftp()
        try:
            sftp.get("/root/omnilive-debug.apk", str(ARTIFACT_PATH))
        finally:
            sftp.close()

        digest = hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
        with _lock:
            _state.update(status="ready", message="APK ready", finished_at=time.time(), sha256=digest)
        external = os.environ.get("RENDER_EXTERNAL_URL", "https://omnilive-do-build-bridge.onrender.com")
        print(f"OMNILIVE_APK_READY_SHA256={digest}", flush=True)
        print(f"OMNILIVE_APK_URL={external}/artifact?token={CONTROL_TOKEN}", flush=True)
    except Exception as exc:
        _append_log(f"\nERROR: {exc}\n")
        print(f"OMNILIVE_BUILD_FAILED={exc}", flush=True)
        with _lock:
            _state.update(status="failed", message=str(exc), finished_at=time.time())
    finally:
        if client:
            client.close()
        if source:
            shutil.rmtree(source, ignore_errors=True)


def _deploy_masanawa_relay(host):
    client = None
    try:
        relay_token = os.environ.get("MASANAWA_RELAY_TOKEN", "").strip()
        if not relay_token:
            raise RuntimeError("MASANAWA_RELAY_TOKEN is not configured")
        with _lock:
            _state.update(status="deploying_relay", message="deploying Masanawa Flutterwave relay", started_at=time.time(), finished_at=None, host=host, log="")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username="root",
            pkey=SSH_KEY,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )

        relay_py = r'''#!/usr/bin/env python3
import http.server
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TOKEN = os.environ["MASANAWA_RELAY_TOKEN"]
ALLOWED = [
    re.compile(r"^/v3/top-bill-categories$"),
    re.compile(r"^/v3/bills/[^/]+/billers$"),
    re.compile(r"^/v3/billers/[^/]+/items$"),
    re.compile(r"^/v3/bill-items/[^/]+/validate$"),
    re.compile(r"^/v3/billers/[^/]+/items/[^/]+/payment$"),
    re.compile(r"^/v3/bills/[^/]+$"),
]
MAX_BODY = 1024 * 1024

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MasanawaRelay/1.0"
    def log_message(self, fmt, *args):
        print(json.dumps({"event":"request","message":fmt % args}), flush=True)
    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(data)))
        self.send_header("cache-control","no-store")
        self.end_headers()
        self.wfile.write(data)
    def _handle(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz" and self.command == "GET":
            return self._json(200, {"ok":True,"service":"masanawa-flutterwave-relay"})
        if self.headers.get("x-masanawa-relay-token","") != TOKEN:
            return self._json(401, {"ok":False,"code":"unauthorized"})
        if self.command not in {"GET","POST"} or not any(p.fullmatch(parsed.path) for p in ALLOWED):
            return self._json(404, {"ok":False,"code":"not_allowed"})
        auth = self.headers.get("authorization","")
        if not auth.startswith("Bearer "):
            return self._json(401, {"ok":False,"code":"missing_provider_authorization"})
        body = None
        if self.command == "POST":
            length = int(self.headers.get("content-length","0") or "0")
            if length < 0 or length > MAX_BODY:
                return self._json(413, {"ok":False,"code":"body_too_large"})
            body = self.rfile.read(length) if length else b""
        upstream = "https://api.flutterwave.com" + parsed.path + (("?" + parsed.query) if parsed.query else "")
        req = urllib.request.Request(upstream, data=body, method=self.command, headers={
            "authorization":auth,
            "accept":"application/json",
            **({"content-type":"application/json"} if self.command == "POST" else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = resp.read()
                status = resp.status
                ctype = resp.headers.get("content-type","application/json")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            ctype = exc.headers.get("content-type","application/json")
        except Exception:
            return self._json(502, {"ok":False,"code":"flutterwave_unreachable"})
        self.send_response(status)
        self.send_header("content-type",ctype)
        self.send_header("content-length",str(len(payload)))
        self.send_header("cache-control","no-store")
        self.end_headers()
        self.wfile.write(payload)
    do_GET = _handle
    do_POST = _handle

http.server.ThreadingHTTPServer(("127.0.0.1",8090), Handler).serve_forever()
'''
        nowpayments_py = r'''#!/usr/bin/env python3
import hashlib
import http.server
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TOKEN = os.environ["MASANAWA_RELAY_TOKEN"]
MAX_BODY = 512 * 1024
ALLOWED = [
    ("GET", re.compile(r"^/v1/status$")),
    ("GET", re.compile(r"^/v1/currencies$")),
    ("GET", re.compile(r"^/v1/min-amount$")),
    ("GET", re.compile(r"^/v1/estimate$")),
    ("POST", re.compile(r"^/v1/invoice$")),
    ("POST", re.compile(r"^/v1/payment$")),
    ("GET", re.compile(r"^/v1/payment/?$")),
    ("GET", re.compile(r"^/v1/payment/[A-Za-z0-9_-]{1,128}$")),
    ("POST", re.compile(r"^/v1/auth$")),
    ("GET", re.compile(r"^/v1/balance$")),
    ("POST", re.compile(r"^/v1/conversion$")),
    ("GET", re.compile(r"^/v1/conversion/[A-Za-z0-9_-]{1,128}$")),
    ("POST", re.compile(r"^/v1/payout/validate-address$")),
    ("GET", re.compile(r"^/v1/payout/fee$")),
    ("GET", re.compile(r"^/v1/payout-withdrawal/min-amount/[A-Za-z0-9_-]{2,32}$")),
    ("POST", re.compile(r"^/v1/payout$")),
    ("GET", re.compile(r"^/v1/payout$")),
    ("POST", re.compile(r"^/v1/payout/[A-Za-z0-9_-]{1,128}/verify$")),
]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = urllib.request.build_opener(NoRedirect)

def token_ok(candidate):
    if not candidate or not TOKEN:
        return False
    return secrets.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(TOKEN.encode()).digest(),
    )

def allowed(method, path):
    return any(method == candidate and pattern.fullmatch(path) for candidate, pattern in ALLOWED)

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MasanawaNowPaymentsRelay/1.0"
    def log_message(self, fmt, *args):
        print(json.dumps({"event":"request","message":fmt % args}), flush=True)
    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(data)))
        self.send_header("cache-control","no-store")
        self.end_headers()
        self.wfile.write(data)
    def _handle(self):
        parsed = urlsplit(self.path)
        if self.command == "GET" and parsed.path == "/healthz":
            return self._json(200, {"ok":True,"service":"masanawa-nowpayments-relay"})
        if not token_ok(self.headers.get("x-masanawa-relay-token","")):
            return self._json(401, {"ok":False,"code":"unauthorized"})
        if not allowed(self.command, parsed.path):
            return self._json(404, {"ok":False,"code":"not_allowed"})
        body = None
        if self.command == "POST":
            try:
                length = int(self.headers.get("content-length","0") or "0")
            except ValueError:
                return self._json(400, {"ok":False,"code":"invalid_content_length"})
            if length < 0 or length > MAX_BODY:
                return self._json(413, {"ok":False,"code":"body_too_large"})
            body = self.rfile.read(length) if length else b""
        headers = {"accept": self.headers.get("accept","application/json")}
        api_key = self.headers.get("x-api-key","")
        authorization = self.headers.get("authorization","")
        content_type = self.headers.get("content-type","")
        if api_key:
            headers["x-api-key"] = api_key
        if authorization:
            headers["authorization"] = authorization
        if content_type and self.command == "POST":
            headers["content-type"] = content_type
        upstream = "https://api.nowpayments.io" + parsed.path + (("?" + parsed.query) if parsed.query else "")
        req = urllib.request.Request(upstream, data=body, method=self.command, headers=headers)
        try:
            with OPENER.open(req, timeout=20) as resp:
                payload = resp.read()
                status = resp.status
                ctype = resp.headers.get("content-type","application/json")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            ctype = exc.headers.get("content-type","application/json")
        except Exception:
            return self._json(502, {"ok":False,"code":"nowpayments_unreachable"})
        self.send_response(status)
        self.send_header("content-type",ctype)
        self.send_header("content-length",str(len(payload)))
        self.send_header("cache-control","no-store")
        self.end_headers()
        self.wfile.write(payload)
    do_GET = _handle
    do_POST = _handle

http.server.ThreadingHTTPServer(("127.0.0.1",8091), Handler).serve_forever()
'''

        sftp = client.open_sftp()
        try:
            with sftp.file("/usr/local/bin/masanawa-flutterwave-relay.py", "w") as remote:
                remote.write(relay_py)
            sftp.chmod("/usr/local/bin/masanawa-flutterwave-relay.py", 0o755)
            with sftp.file("/usr/local/bin/masanawa-nowpayments-relay.py", "w") as remote:
                remote.write(nowpayments_py)
            sftp.chmod("/usr/local/bin/masanawa-nowpayments-relay.py", 0o755)
        finally:
            sftp.close()

        escaped = relay_token.replace("\\", "\\\\").replace('"', '\\"')
        service = f'''[Unit]
Description=Masanawa Flutterwave egress relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="MASANAWA_RELAY_TOKEN={escaped}"
ExecStart=/usr/bin/python3 /usr/local/bin/masanawa-flutterwave-relay.py
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
        nowpayments_service = f'''[Unit]
Description=Masanawa NOWPayments fixed-egress relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="MASANAWA_RELAY_TOKEN={escaped}"
ExecStart=/usr/bin/python3 /usr/local/bin/masanawa-nowpayments-relay.py
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
        domain = host.replace(".", "-") + ".sslip.io"
        caddyfile = f'''{domain} {{
    encode gzip
    handle /v1/* {{
        reverse_proxy 127.0.0.1:8091
    }}
    handle {{
        reverse_proxy 127.0.0.1:8090
    }}
}}
'''
        sftp = client.open_sftp()
        try:
            with sftp.file("/etc/systemd/system/masanawa-flutterwave-relay.service", "w") as remote:
                remote.write(service)
            with sftp.file("/etc/systemd/system/masanawa-nowpayments-relay.service", "w") as remote:
                remote.write(nowpayments_service)
            with sftp.file("/tmp/Caddyfile.masanawa", "w") as remote:
                remote.write(caddyfile)
        finally:
            sftp.close()

        _run(client, "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y caddy ca-certificates python3", timeout=900)
        _run(client, "install -m 0644 /tmp/Caddyfile.masanawa /etc/caddy/Caddyfile && systemctl daemon-reload && systemctl enable --now masanawa-flutterwave-relay masanawa-nowpayments-relay && systemctl restart masanawa-flutterwave-relay masanawa-nowpayments-relay && caddy validate --config /etc/caddy/Caddyfile && systemctl enable caddy && systemctl restart caddy", timeout=180)
        _run(client, "systemctl is-active masanawa-flutterwave-relay && systemctl is-active masanawa-nowpayments-relay && systemctl is-active caddy && curl -fsS http://127.0.0.1:8090/healthz && curl -fsS http://127.0.0.1:8091/healthz", timeout=120)
        with _lock:
            _state.update(status="relay_ready", message=f"Masanawa Flutterwave + NOWPayments relays ready at https://{domain}", finished_at=time.time(), host=host)
        print(f"MASANAWA_RELAY_READY=https://{domain}", flush=True)
        print(f"MASANAWA_NOWPAYMENTS_RELAY_READY=https://{domain}/v1", flush=True)
    except Exception as exc:
        _append_log(f"\nERROR: {exc}\n")
        with _lock:
            _state.update(status="failed", message=str(exc), finished_at=time.time())
    finally:
        if client:
            client.close()


def _publish_existing_artifact(host):
    client = None
    try:
        if not ARTIFACT_SHARE_TOKEN:
            raise RuntimeError("artifact share token is not configured")
        with _lock:
            _state.update(status="publishing", message="retrieving existing APK from DigitalOcean", started_at=time.time(), finished_at=None, host=host, log="")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username="root",
            pkey=SSH_KEY,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        sftp = client.open_sftp()
        try:
            sftp.get("/root/omnilive-debug.apk", str(ARTIFACT_PATH))
        finally:
            sftp.close()
        digest = hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
        with _lock:
            _state.update(status="ready", message="APK ready for artifact-only sharing", finished_at=time.time(), sha256=digest)
        print(f"OMNILIVE_ARTIFACT_PUBLISHED_SHA256={digest}", flush=True)
    except Exception as exc:
        _append_log(f"\nERROR: {exc}\n")
        with _lock:
            _state.update(status="failed", message=str(exc), finished_at=time.time())
    finally:
        if client:
            client.close()

COMMAND_URL = "https://raw.githubusercontent.com/Baatiku/candlefollowOrigin/omnilive-build-bridge/omnilive-build-bridge/command.json"
_last_command_id = None

def _poll_commands():
    global _last_command_id
    while True:
        try:
            with urllib.request.urlopen(f"{COMMAND_URL}?t={time.time_ns()}", timeout=15) as response:
                command = json.loads(response.read().decode())
            command_id = command.get("command_id")
            if command_id and command_id != _last_command_id:
                _last_command_id = command_id
                action = command.get("action")
                if action in {"build", "publish_artifact", "deploy_masanawa_relay"}:
                    host = command.get("host", "")
                    ipaddress.ip_address(host)
                    with _lock:
                        busy = _state["status"] in {"restoring", "uploading", "building", "publishing", "deploying_relay"}
                        if action == "build" and not busy:
                            _state.update(status="queued", message="build queued from command mailbox", started_at=time.time(), finished_at=None, host=host, sha256=None, log="")
                    if busy:
                        print(f"OMNILIVE_COMMAND_SKIPPED_BUSY={command_id}", flush=True)
                    elif action == "build":
                        print(f"OMNILIVE_COMMAND_ACCEPTED={command_id} host={host}", flush=True)
                        threading.Thread(target=_build, args=(host,), daemon=True).start()
                    elif action == "deploy_masanawa_relay":
                        print(f"MASANAWA_RELAY_COMMAND_ACCEPTED={command_id} host={host}", flush=True)
                        threading.Thread(target=_deploy_masanawa_relay, args=(host,), daemon=True).start()
                    else:
                        print(f"OMNILIVE_ARTIFACT_COMMAND_ACCEPTED={command_id} host={host}", flush=True)
                        threading.Thread(target=_publish_existing_artifact, args=(host,), daemon=True).start()
        except Exception as exc:
            print(f"OMNILIVE_COMMAND_POLL_ERROR={type(exc).__name__}:{exc}", flush=True)
        time.sleep(5)


@app.get("/deploy-masanawa-relay")
def deploy_masanawa_relay():
    supplied = request.args.get("token", "")
    expected = os.environ.get("MASANAWA_RELAY_TOKEN", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        return jsonify(error="unauthorized"), 401
    host = request.args.get("host", "")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return jsonify(error="invalid host"), 400
    with _lock:
        if _state["status"] in {"restoring", "uploading", "building", "publishing", "deploying_relay"}:
            return jsonify(_state), 409
        _state.update(status="queued", message="Masanawa relay deployment queued", started_at=time.time(), finished_at=None, host=host, log="")
    threading.Thread(target=_deploy_masanawa_relay, args=(host,), daemon=True).start()
    return jsonify(status="queued", host=host), 202

@app.get("/health")
def health():
    return jsonify(ok=True, service="omnilive-build-bridge", activated=True, stable_credentials=True)

@app.get("/bootstrap")
def bootstrap():
    return jsonify(ssh_public_key=SSH_PUBLIC_TEXT, stable_credentials=True)

@app.get("/build")
def build():
    if not _auth():
        return jsonify(error="unauthorized"), 401
    host = request.args.get("host", "")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return jsonify(error="invalid host"), 400

    with _lock:
        if _state["status"] in {"restoring", "uploading", "building"}:
            return jsonify(_state), 409
        if ARTIFACT_PATH.exists():
            ARTIFACT_PATH.unlink()
        _state.update(status="queued", message="build queued", started_at=time.time(), finished_at=None, host=host, sha256=None, log="")
    threading.Thread(target=_build, args=(host,), daemon=True).start()
    return jsonify(status="queued", host=host), 202

@app.get("/status")
def status():
    if not _auth():
        return jsonify(error="unauthorized"), 401
    with _lock:
        return jsonify(dict(_state))

@app.get("/artifact")
def artifact():
    if not _auth():
        return jsonify(error="unauthorized"), 401
    with _lock:
        ready = _state["status"] == "ready"
    if not ready or not ARTIFACT_PATH.exists():
        return jsonify(error="artifact not ready"), 404
    return send_file(ARTIFACT_PATH, mimetype="application/vnd.android.package-archive", as_attachment=True, download_name="omnilive-debug.apk")

@app.get("/share/<token>")
def share_artifact_page(token):
    if not ARTIFACT_SHARE_TOKEN or not secrets.compare_digest(token, ARTIFACT_SHARE_TOKEN):
        return jsonify(error="not found"), 404
    with _lock:
        ready = _state["status"] == "ready"
        digest = _state.get("sha256")
    if not ready or not ARTIFACT_PATH.exists():
        return jsonify(error="artifact not ready"), 404
    html = (
        "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>OmniLive APK</title></head><body>"
        "<h1>OmniLive debug APK</h1>"
        f"<p>SHA-256: <code>{digest}</code></p>"
        f"<p><a href=\"/share/{token}/apk\">Download omnilive-debug.apk</a></p>"
        "</body></html>"
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.get("/share/<token>/apk")
def share_artifact_file(token):
    if not ARTIFACT_SHARE_TOKEN or not secrets.compare_digest(token, ARTIFACT_SHARE_TOKEN):
        return jsonify(error="not found"), 404
    with _lock:
        ready = _state["status"] == "ready"
    if not ready or not ARTIFACT_PATH.exists():
        return jsonify(error="artifact not ready"), 404
    return send_file(ARTIFACT_PATH, mimetype="application/vnd.android.package-archive", as_attachment=True, download_name="omnilive-debug.apk")



def _auto_deploy_masanawa_relay():
    host = os.environ.get("MASANAWA_RELAY_DEPLOY_HOST", "").strip()
    if not host:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        print("MASANAWA_RELAY_AUTO_DEPLOY_INVALID_HOST", flush=True)
        return
    time.sleep(2)
    print(f"MASANAWA_RELAY_AUTO_DEPLOY host={host}", flush=True)
    _deploy_masanawa_relay(host)

# Start the GitHub command mailbox poller when Gunicorn imports this module.
threading.Thread(target=_poll_commands, daemon=True).start()
threading.Thread(target=_auto_deploy_masanawa_relay, daemon=True).start()
