import os, re, sys, shutil, tempfile, socket, ipaddress, time, threading, uuid, subprocess, zipfile
import base64, hmac, signal, importlib, webbrowser
from importlib import metadata
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template, send_file
import yt_dlp

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
if not os.path.isdir(template_dir):
    template_dir = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=template_dir)

LOCAL = os.getenv("LOCAL_MODE") == "1"
MAX_MINUTES = int(os.getenv("MAX_MINUTES", "600" if LOCAL else "45"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "100000" if LOCAL else "30"))
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "200" if LOCAL else "50"))
JOB_TTL = (60 if LOCAL else 15) * 60
MAX_JOBS = int(os.getenv("MAX_JOBS", "6" if LOCAL else "4"))

ACCESS_CODE = os.getenv("ACCESS_CODE", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
UPDATE_HOURS = float(os.getenv("UPDATE_HOURS", "0" if LOCAL else "24"))
AUTO_RESTART = os.getenv("AUTO_RESTART") == "1"

_hits, _jobs = {}, {}
_lock = threading.Lock()

# ---------- কুকি (সার্ভার সিক্রেট বা ব্রাউজার থেকে) ----------
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
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        COOKIE_FILE = path
_init_cookies()

def cookie_copy(cdir):
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        return None
    dst = os.path.join(cdir, "cookies.txt")
    shutil.copyfile(COOKIE_FILE, dst)
    try:
        os.chmod(dst, 0o600)
    except Exception:
        pass
    return dst

# FFmpeg লোকেশন নির্ধারণ
FFMPEG_EXE = None
if shutil.which("ffmpeg"):
    FFMPEG_EXE = "ffmpeg"
else:
    # Windows WinGet বা কমন লোকেশন চেক
    winget_path = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe")
    if os.path.exists(winget_path):
        FFMPEG_EXE = winget_path
    else:
        try:
            import imageio_ffmpeg
            FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            FFMPEG_EXE = None

COOKIES_BROWSER = os.getenv("COOKIES_BROWSER", "")
def apply_cookies(opts, path):
    if path and os.path.exists(path):
        opts["cookiefile"] = path
    elif COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)

def check_access():
    if not ACCESS_CODE:
        return True
    given = (request.json or {}).get("code", "")
    return hmac.compare_digest(str(given), ACCESS_CODE)

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

def extract_clean_url(text):
    if not text:
        return ""
    m = re.search(r'https?://[^\s<>"]+', text)
    return m.group(0) if m else text.strip()

def detect_platform(url):
    h = (urlparse(url).hostname or "").lower()
    if any(x in h for x in ("youtube.com", "youtu.be")): return "youtube"
    if any(x in h for x in ("twitter.com", "x.com")): return "twitter"
    if any(x in h for x in ("facebook.com", "fb.watch", "fb.com")): return "facebook"
    if "tiktok.com" in h: return "tiktok"
    if "instagram.com" in h: return "instagram"
    if any(x in h for x in ("pinterest.com", "pin.it")): return "pinterest"
    if "reddit.com" in h: return "reddit"
    return "generic"

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
}

# ---------- ডাউনলোড ওয়ার্কার ----------
def run_video(job, url, quality):
    tmp = job["dir"]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = min(90, int(d.get("downloaded_bytes", 0) * 90 / total))
            job["message"] = f"ডাউনলোড হচ্ছে... ({job['progress']}%)"
        elif d.get("status") == "finished":
            job["progress"] = 92
            job["message"] = "প্রসেস হচ্ছে (FFmpeg মার্জ/কনভার্ট)..."

    opts = dict(
        BASE_OPTS,
        outtmpl=os.path.join(tmp, "%(title).80s.%(ext)s"),
        merge_output_format="mp4",
        progress_hooks=[hook],
        match_filter=yt_dlp.utils.match_filter_func(f"duration <? {MAX_MINUTES * 60} & !is_live")
    )
    apply_cookies(opts, job.get("cookie"))
    if FFMPEG_EXE:
        opts["ffmpeg_location"] = FFMPEG_EXE

    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320"
        }]
    else:
        q = int(re.sub(r"\D", "", quality) or 1080)
        if q >= 1080:
            opts["format"] = "bv*[height<=1080]+ba/bv*[width<=1080]+ba/bestvideo+bestaudio/best"
        else:
            opts["format"] = f"bv*[height<={q}]+ba/bv*[width<={q}]+ba/b[height<={q}]/b[width<={q}]/best"

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = [f for f in os.listdir(tmp) if os.path.isfile(os.path.join(tmp, f))]
    if not files:
        raise RuntimeError(f"ভিডিও {MAX_MINUTES} মিনিটের বেশি বা ডাউনলোড করা সম্ভব হয়নি।")
    return os.path.join(tmp, files[0])

def run_images(job, url):
    tmp = job["dir"]
    job["message"] = "ছবি খোঁজা ও এইচডি আকারে ডাউনলোড হচ্ছে..."
    job["progress"] = 25

    gdl_bin = shutil.which("gallery-dl")
    if gdl_bin:
        cmd = [gdl_bin]
    else:
        cmd = [sys.executable, "-m", "gallery_dl"]

    cmd += ["-D", tmp, "--range", f"1-{MAX_IMAGES}"]
    if job.get("cookie"):
        cmd += ["--cookies", job["cookie"]]
    elif COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_BROWSER]

    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=300)

    # সাবফোল্ডারসহ সকল ডাউনলোডকৃত ফাইল সংগ্রহ (Twitter, Instagram ইত্যাদির জন্য)
    all_files = []
    for root, _, filenames in os.walk(tmp):
        for fn in filenames:
            if not fn.endswith(".zip"):
                all_files.append(os.path.join(root, fn))

    if not all_files:
        raise RuntimeError("কোনো ছবি পাওয়া যায়নি। সাইটটিতে লগইন কুকি প্রয়োজন হতে পারে।")

    # ফাইলনেমে উইন্ডোজের জন্য ক্ষতিকর ক্যারেক্টার পরিষ্কার
    clean_files = []
    for fpath in all_files:
        dirname, fname = os.path.split(fpath)
        nf = re.sub(r'[<>:"/\\|?*]', "_", fname)
        target_path = os.path.join(tmp, nf)
        if fpath != target_path:
            shutil.move(fpath, target_path)
        clean_files.append(target_path)

    if len(clean_files) == 1:
        return clean_files[0]

    job["message"] = "সকল এইচডি ছবি জিপ (ZIP) করা হচ্ছে..."
    job["progress"] = 90
    zpath = os.path.join(tmp, "hd_images.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for cf in clean_files:
            z.write(cf, os.path.basename(cf))
    return zpath

def worker(jid, url, mode, quality):
    job = _jobs.get(jid)
    if not job:
        return
    try:
        path = run_images(job, url) if mode == "image" else run_video(job, url, quality)
        job.update(status="done", progress=100, message="ডাউনলোড সম্পন্ন!", path=path)
    except subprocess.TimeoutExpired:
        job.update(status="error", message="সময় শেষ হয়ে গেছে (টাইমআউট)।")
    except Exception as e:
        job.update(status="error", message=str(e)[:220])
    finally:
        shutil.rmtree(job.get("cdir", ""), ignore_errors=True)

# ---------- রুটসমূহ ----------
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/info")
def info():
    if not rate_ok(client_ip()):
        return jsonify(error="অতিরিক্ত অনুরোধ! কিছুক্ষণ পর চেষ্টা করুন।"), 429
    if not check_access():
        return jsonify(error="অ্যাক্সেস কোড ভুল।"), 401

    raw_url = (request.json or {}).get("url", "")
    url = extract_clean_url(raw_url)
    if not safe_url(url):
        return jsonify(error="অবৈধ বা অসমর্থিত লিংক। সঠিক URL দিন।"), 400

    cdir = tempfile.mkdtemp(prefix="ck_")
    platform = detect_platform(url)
    try:
        opts = dict(BASE_OPTS)
        apply_cookies(opts, cookie_copy(cdir))
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception as e:
        err_msg = str(e)
        if "Sign in to confirm" in err_msg or "bot" in err_msg.lower():
            return jsonify(error="লগইন ভেরিফিকেশন প্রয়োজন। অ্যাপটি নিজের কম্পিউটারে চালিয়ে সরাসরি কুকিসহ ব্যবহার করুন (README দেখুন)।"), 422
        return jsonify(error=f"তথ্য আনা যায়নি: {err_msg[:200]}"), 422
    finally:
        shutil.rmtree(cdir, ignore_errors=True)

    # স্টোরিবোর্ড (sb0, sb1, sb2) এবং অডিও-অনলি বাদ দিয়ে সঠিক ভিডিও রেজোলিউশন ফিল্টার
    valid_dims = []
    for f in d.get("formats", []):
        fid = str(f.get("format_id", ""))
        vcodec = f.get("vcodec")
        ext = f.get("ext")
        protocol = str(f.get("protocol", ""))

        if fid.startswith("sb") or ext == "mhtml" or protocol == "mhtml":
            continue
        if vcodec in (None, "none", "images"):
            continue

        h = f.get("height")
        w = f.get("width")
        if h or w:
            dim = min(h, w) if (h and w) else (h or w)
            valid_dims.append(dim)

    dims = sorted(set(valid_dims), reverse=True)
    resolutions = []
    seen = set()

    for dim in dims:
        if dim >= 950 and "1080" not in seen:
            resolutions.append({"height": 1080, "label": "1080p", "badge": "Full HD", "desc": "সর্বোচ্চ স্পষ্টতা"})
            seen.add("1080")
        elif dim >= 650 and dim < 950 and "720" not in seen:
            resolutions.append({"height": 720, "label": "720p", "badge": "HD", "desc": "এইচডি কোয়ালিটি"})
            seen.add("720")
        elif dim >= 420 and dim < 650 and "480" not in seen:
            resolutions.append({"height": 480, "label": "480p", "badge": "SD", "desc": "স্ট্যান্ডার্ড"})
            seen.add("480")
        elif dim >= 250 and dim < 420 and "360" not in seen:
            resolutions.append({"height": 360, "label": "360p", "badge": "SD", "desc": "ডাটা সেভার"})
            seen.add("360")

    # যদি ভিডিও ফরম্যাট ফাঁকা থাকে (ক্লাউড ব্লক বা রেস্ট্রিকশন), স্ট্যান্ডার্ড অপশন সরবরাহ
    if not resolutions:
        resolutions = [
            {"height": 1080, "label": "1080p", "badge": "Full HD", "desc": "সর্বোচ্চ কোয়ালিটি"},
            {"height": 720, "label": "720p", "badge": "HD", "desc": "এইচডি কোয়ালিটি"},
            {"height": 480, "label": "480p", "badge": "SD", "desc": "স্ট্যান্ডার্ড"},
            {"height": 360, "label": "360p", "badge": "SD", "desc": "ডাটা সেভার"}
        ]

    return jsonify(
        platform=platform,
        title=d.get("title") or "মিডিয়া ফাইল",
        thumbnail=d.get("thumbnail") or "",
        uploader=d.get("uploader") or d.get("channel") or "",
        duration=d.get("duration") or 0,
        resolutions=resolutions,
        has_audio=True
    )

@app.post("/api/start")
def start():
    cleanup_jobs()
    if not check_access():
        return jsonify(error="অ্যাক্সেস কোড ভুল।"), 401
    if not rate_ok(client_ip()):
        return jsonify(error="অতিরিক্ত অনুরোধ! কিছুক্ষণ পর চেষ্টা করুন।"), 429

    b = request.json or {}
    url = extract_clean_url(b.get("url", ""))
    mode = b.get("mode", "video")
    quality = str(b.get("quality", "1080"))

    if mode not in ("video", "image") or not safe_url(url):
        return jsonify(error="অবৈধ অনুরোধ। সঠিক URL দিন।"), 400

    with _lock:
        if sum(1 for j in _jobs.values() if j["status"] == "running") >= MAX_JOBS:
            return jsonify(error="সার্ভার এখন ব্যস্ত, অনুগ্রহ করে ১ মিনিট পর চেষ্টা করুন।"), 503

        jid = uuid.uuid4().hex
        cdir = tempfile.mkdtemp(prefix="ck_")
        _jobs[jid] = dict(
            status="running",
            progress=5,
            message="শুরু হচ্ছে...",
            created=time.time(),
            dir=tempfile.mkdtemp(prefix="dl_"),
            cdir=cdir,
            cookie=cookie_copy(cdir)
        )

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
        return jsonify(error="ফাইল এখনও প্রস্তুত নয়।"), 404
    fpath = j["path"]
    return send_file(fpath, as_attachment=True, download_name=os.path.basename(fpath))

# ---------- ভার্সন ও অটো আপডেট ----------
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
    if not AUTO_RESTART:
        return
    for _ in range(360):
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
    return jsonify(ok=True, mode="local" if LOCAL else "cloud", ffmpeg=bool(FFMPEG_EXE), versions=get_ver())

@app.get("/api/config")
def config():
    return jsonify(code_required=bool(ACCESS_CODE), max_minutes=MAX_MINUTES, local=LOCAL)

@app.post("/api/admin/update")
def admin_update():
    tok = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(tok, ADMIN_TOKEN):
        return jsonify(error="অননুমোদিত"), 404
    before = get_ver()
    ok, err = do_update()
    after = get_ver()
    if ok and after != before:
        threading.Thread(target=restart_when_idle, daemon=True).start()
    return jsonify(ok=ok, before=before, after=after, error=None if ok else err)

HOST = os.getenv("HOST", "127.0.0.1" if LOCAL else "0.0.0.0")

# Telegram মডিউল ইন্টিগ্রেশন
try:
    import tg
    TG_ENABLED = tg.init_telegram(app, _jobs, _lock, MAX_JOBS, ACCESS_CODE, LOCAL, HOST)
except Exception:
    TG_ENABLED = False

def lan_ip():
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return None

if __name__ == "__main__":
    host = HOST
    port = int(os.getenv("PORT", 7860))
    print("\n==============================================")
    print(f" Local App   : http://127.0.0.1:{port}")
    if host == "0.0.0.0" and lan_ip():
        print(f" Phone (Wi-Fi): http://{lan_ip()}:{port}")
    print(" FFmpeg      :", "Ready" if FFMPEG_EXE else "Missing (fallback to direct stream)")
    print(" Telegram    :", "Enabled" if TG_ENABLED else "Local only")
    print(" Stop        : Press Ctrl+C")
    print("==============================================\n")
    if os.getenv("OPEN_BROWSER") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host=host, port=port, threaded=True)
