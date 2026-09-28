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

CONTROL_TOKEN = secrets.token_urlsafe(32)
PAYLOAD_KEY_HEX = os.environ.get("PAYLOAD_KEY_HEX", "")
PAYLOAD_KEY = bytes.fromhex(PAYLOAD_KEY_HEX) if PAYLOAD_KEY_HEX else None
SSH_KEY = paramiko.RSAKey.generate(3072)
SSH_PUBLIC_TEXT = f"{SSH_KEY.get_name()} {SSH_KEY.get_base64()} omnilive-render-bridge"
print(f"OMNILIVE_BRIDGE_TOKEN={CONTROL_TOKEN}", flush=True)
print(f"OMNILIVE_SSH_PUBLIC_KEY={SSH_PUBLIC_TEXT}", flush=True)

app = Flask(__name__)
_lock = threading.Lock()
_state = {
    "status": "idle",
    "message": "ready; ephemeral relay credentials loaded",
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
    env_manifest = os.environ.get("OMNILIVE_ENV_MANIFEST_JSON")
    if env_manifest:
        payload = json.loads(env_manifest)
        root = Path(tempfile.mkdtemp(prefix="omnilive-src-"))
        for item in payload["files"]:
            path = item["path"]
            value = os.environ.get(item["env"])
            if value is None:
                raise RuntimeError(f"missing Render secret for {path}")
            plain = bytes.fromhex(value)
            actual = _git_blob_sha(plain)
            if actual != item["git_sha1"]:
                raise RuntimeError(f"secret payload integrity failure for {path}")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plain)
        return root

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
        _run(client, f"cd {remote_root} && docker build --progress=plain -f ops/apk-builder.Dockerfile -t omnilive-apk .", timeout=3600)
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
                if command.get("action") == "build":
                    host = command.get("host", "")
                    ipaddress.ip_address(host)
                    with _lock:
                        busy = _state["status"] in {"restoring", "uploading", "building"}
                        if not busy:
                            _state.update(status="queued", message="build queued from command mailbox", started_at=time.time(), finished_at=None, host=host, sha256=None, log="")
                    if busy:
                        print(f"OMNILIVE_COMMAND_SKIPPED_BUSY={command_id}", flush=True)
                    else:
                        print(f"OMNILIVE_COMMAND_ACCEPTED={command_id} host={host}", flush=True)
                        threading.Thread(target=_build, args=(host,), daemon=True).start()
        except Exception as exc:
            print(f"OMNILIVE_COMMAND_POLL_ERROR={type(exc).__name__}:{exc}", flush=True)
        time.sleep(5)

@app.get("/health")
def health():
    return jsonify(ok=True, service="omnilive-build-bridge", env_payload=bool(os.environ.get("OMNILIVE_ENV_MANIFEST_JSON")))

@app.get("/bootstrap")
def bootstrap():
    return jsonify(ssh_public_key=SSH_PUBLIC_TEXT, stable_credentials=False)

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


# Start the GitHub command mailbox poller when Gunicorn imports this module.
threading.Thread(target=_poll_commands, daemon=True).start()
