import os, re, sys, shutil, tempfile, socket, ipaddress, time, threading, uuid, subprocess, zipfile
import base64, hmac, signal, importlib
from importlib import metadata
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template, send_file
import yt_dlp

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
if not os.path.isdir(template_dir):
    template_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=template_dir)

MAX_MINUTES = int(os.getenv("MAX_MINUTES", "30"))   # ভিডিওর দৈর্ঘ্যের সীমা
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))      # প্রতি ঘণ্টায় প্রতি IP
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "30"))      # এক লিংক থেকে সর্বোচ্চ ছবি
JOB_TTL = 15 * 60                                    # ১৫ মিনিট পর ফাইল মুছবে
MAX_JOBS = 3                                         # একসাথে চলা জবের সীমা

ACCESS_CODE = os.getenv("ACCESS_CODE", "")           # সেট করলে /api/info ও /api/start-এ কোড লাগবে
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")           # সেট করলে /api/admin/update চালু
UPDATE_HOURS = float(os.getenv("UPDATE_HOURS", "24"))  # কত ঘণ্টা পর পর yt-dlp আপডেট (0 = বন্ধ)
AUTO_RESTART = os.getenv("AUTO_RESTART") == "1"       # আপডেটের পর gunicorn রিলোড

_hits, _jobs = {}, {}
_lock = threading.Lock()

# ---------- কুকি (শুধু সার্ভারের সিক্রেট থেকে; ইউজার আপলোড নয়) ----------
COOKIE_FILE = None
def _init_cookies():
    global COOKIE_FILE
    raw = os.getenv("COOKIES_TXT", "")
    if os.getenv("COOKIES_B64"):
        try:
            raw = base64.b64decode(os.environ["COOKIES_B64"]).decode("utf-8")
        except Exception:
            raw = ""
    if raw.strip():
        path = os.path.join(tempfile.gettempdir(), "server_cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw.replace("\\n", "\n"))
        os.chmod(path, 0o600)
        COOKIE_FILE = path
_init_cookies()

def cookie_copy(cdir):
    """প্রতি জবের জন্য কুকির আলাদা কপি (yt-dlp ফাইলে লিখতে পারে)।"""
    if not COOKIE_FILE:
        return None
    dst = os.path.join(cdir, "cookies.txt")
    shutil.copyfile(COOKIE_FILE, dst)
    os.chmod(dst, 0o600)
    return dst

def check_access():
    if not ACCESS_CODE:
        return True
    given = (request.json or {}).get("code", "")
    return hmac.compare_digest(str(given), ACCESS_CODE)

# ---------- সহায়ক ----------
def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()

def rate_ok(ip):
    now = time.time()
    with _lock:
        hits = [t for t in _hits.get(ip, []) if now - t < 3600]
        ok = len(hits) < RATE_LIMIT
        if ok:
            hits.append(now)
        _hits[ip] = hits
        return ok

def safe_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except socket.gaierror:
        return False
    return True

def cleanup_jobs():
    now = time.time()
    with _lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            shutil.rmtree(_jobs[jid].get("dir", ""), ignore_errors=True)
            shutil.rmtree(_jobs[jid].get("cdir", ""), ignore_errors=True)
            del _jobs[jid]

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "mweb"]
        }
    }
}

# ---------- ওয়ার্কার ----------
def run_video(job, url, quality):
    tmp = job["dir"]

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = min(90, int(d["downloaded_bytes"] * 90 / total))
            job["message"] = "ডাউনলোড হচ্ছে..."
        elif d["status"] == "finished":
            job["progress"] = 92
            job["message"] = "প্রসেস হচ্ছে (মার্জ/কনভার্ট)..."

    opts = dict(BASE_OPTS, outtmpl=os.path.join(tmp, "%(title).80s.%(ext)s"),
                merge_output_format="mp4", progress_hooks=[hook],
                match_filter=yt_dlp.utils.match_filter_func(f"duration < {MAX_MINUTES * 60}"))
    if job.get("cookie"):
        opts["cookiefile"] = job["cookie"]
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        q = int(re.sub(r"\D", "", quality) or 1080)
        opts["format"] = f"bv*[height<={q}]+ba/b[height<={q}]/b"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [os.path.join(tmp, f) for f in os.listdir(tmp) if os.path.isfile(os.path.join(tmp, f)) and not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError(f"ভিডিও {MAX_MINUTES} মিনিটের বেশি বা ডাউনলোডযোগ্য নয়।")
    return files[0]

def run_images(job, url):
    tmp = job["dir"]
    job["message"] = "ছবি খোঁজা ও ডাউনলোড হচ্ছে..."
    job["progress"] = 20
    cmd = ["gallery-dl", "-D", tmp, "--range", f"1-{MAX_IMAGES}"]
    if job.get("cookie"):
        cmd += ["--cookies", job["cookie"]]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=300)
    files = []
    for root, _, filenames in os.walk(tmp):
        for f in filenames:
            if not f.endswith((".zip", ".part")):
                files.append(os.path.join(root, f))
    files = sorted(files)
    if not files:
        raise RuntimeError("কোনো ছবি পাওয়া যায়নি। " + (r.stderr or "")[-150:])
    if len(files) == 1:
        return files[0]
    job["message"] = "ZIP তৈরি হচ্ছে..."
    zpath = os.path.join(tmp, "images.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        for fpath in files:
            z.write(fpath, os.path.relpath(fpath, tmp))
    return zpath

def worker(jid, url, mode, quality):
    job = _jobs[jid]
    try:
        path = run_images(job, url) if mode == "image" else run_video(job, url, quality)
        job.update(status="done", progress=100, message="প্রস্তুত!", path=path)
    except subprocess.TimeoutExpired:
        job.update(status="error", message="সময় শেষ হয়ে গেছে।")
    except Exception as e:
        job.update(status="error", message=str(e)[:200])
    finally:
        shutil.rmtree(job.get("cdir", ""), ignore_errors=True)

# ---------- রুট ----------
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/info")
def info():
    if not rate_ok(client_ip()):
        return jsonify(error="অনেক বেশি অনুরোধ। কিছুক্ষণ পরে চেষ্টা করুন।"), 429
    if not check_access():
        return jsonify(error="অ্যাক্সেস কোড ভুল।"), 401
    url = (request.json or {}).get("url", "").strip()
    if not safe_url(url):
        return jsonify(error="অবৈধ বা অনুমোদিত নয় এমন লিংক।"), 400
    cdir = tempfile.mkdtemp(prefix="ck_")
    try:
        opts = dict(BASE_OPTS)
        cf = cookie_copy(cdir)
        if cf:
            opts["cookiefile"] = cf
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=f"তথ্য আনা যায়নি: {str(e)[:200]}"), 422
    finally:
        shutil.rmtree(cdir, ignore_errors=True)
    if not d:
        return jsonify(error="কোনো তথ্য পাওয়া যায়নি।"), 422
    if "entries" in d and d.get("entries"):
        d = next((e for e in d["entries"] if e), d)
    heights = sorted({int(f["height"]) for f in d.get("formats", []) if f.get("height")}, reverse=True)
    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                   duration=d.get("duration"), heights=heights)

@app.post("/api/start")
def start():
    cleanup_jobs()
    if not check_access():
        return jsonify(error="অ্যাক্সেস কোড ভুল।"), 401
    if not rate_ok(client_ip()):
        return jsonify(error="অনেক বেশি অনুরোধ।"), 429
    b = request.json or {}
    url, mode, quality = b.get("url", "").strip(), b.get("mode", "video"), str(b.get("quality", "1080"))
    if mode not in ("video", "image") or not safe_url(url):
        return jsonify(error="অবৈধ অনুরোধ।"), 400
    with _lock:
        if sum(1 for j in _jobs.values() if j["status"] == "running") >= MAX_JOBS:
            return jsonify(error="সার্ভার এখন ব্যস্ত, একটু পরে চেষ্টা করুন।"), 503
        jid = uuid.uuid4().hex
        cdir = tempfile.mkdtemp(prefix="ck_")
        _jobs[jid] = dict(status="running", progress=0, message="শুরু হচ্ছে...",
                          created=time.time(), dir=tempfile.mkdtemp(prefix="dl_"),
                          cdir=cdir, cookie=cookie_copy(cdir))
    threading.Thread(target=worker, args=(jid, url, mode, quality), daemon=True).start()
    return jsonify(job=jid)

@app.get("/api/status/<jid>")
def status(jid):
    j = _jobs.get(jid)
    if not j:
        return jsonify(error="জব পাওয়া যায়নি বা মেয়াদ শেষ।"), 404
    return jsonify(status=j["status"], progress=j["progress"], message=j["message"])

@app.get("/api/file/<jid>")
def file(jid):
    j = _jobs.get(jid)
    if not j or j["status"] != "done":
        return jsonify(error="ফাইল প্রস্তুত নয়।"), 404
    return send_file(j["path"], as_attachment=True, download_name=os.path.basename(j["path"]))

# ---------- ভার্সন, কনফিগ, অটো-আপডেট ----------
def get_ver():
    importlib.invalidate_caches()
    out = {}
    for pkg in ("yt-dlp", "gallery-dl"):
        try:
            out[pkg] = metadata.version(pkg)
        except Exception:
            out[pkg] = "?"
    return out

def do_update():
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--user", "--quiet",
                        "yt-dlp", "gallery-dl"], capture_output=True, text=True, timeout=300)
    return r.returncode == 0, (r.stderr or "")[-200:]

def restart_when_idle():
    """চলমান জব শেষ হলে gunicorn রিলোড, যাতে নতুন ভার্সন লোড হয়।"""
    if not AUTO_RESTART:
        return
    for _ in range(360):  # সর্বোচ্চ ১ ঘণ্টা অপেক্ষা
        if not any(j["status"] == "running" for j in _jobs.values()):
            if hasattr(signal, "SIGHUP"):
                os.kill(os.getppid(), signal.SIGHUP)
            return
        time.sleep(10)

def updater_loop():
    while True:
        time.sleep(UPDATE_HOURS * 3600)
        try:
            before = get_ver()
            ok, _ = do_update()
            if ok and get_ver() != before:
                restart_when_idle()
        except Exception:
            pass

if UPDATE_HOURS > 0:
    threading.Thread(target=updater_loop, daemon=True).start()

@app.get("/health")
def health():
    return jsonify(ok=True, versions=get_ver())

@app.get("/api/config")
def config():
    return jsonify(code_required=bool(ACCESS_CODE), max_minutes=MAX_MINUTES)

@app.post("/api/admin/update")
def admin_update():
    tok = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(tok, ADMIN_TOKEN):
        return jsonify(error="not found"), 404
    before = get_ver()
    ok, err = do_update()
    after = get_ver()
    if ok and after != before:
        threading.Thread(target=restart_when_idle, daemon=True).start()
    return jsonify(ok=ok, before=before, after=after, error=None if ok else err)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 7860)))
