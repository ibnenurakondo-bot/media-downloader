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

MAX_MINUTES = int(os.getenv("MAX_MINUTES", "45"))   # ভিডিওর দৈর্ঘ্যের সীমা (৪৫ মিনিট)
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "20"))      # প্রতি ঘণ্টায় প্রতি IP
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "50"))      # এক লিংক থেকে সর্বোচ্চ ছবি
JOB_TTL = 15 * 60                                    # ১৫ মিনিট পর ফাইল মুছবে
MAX_JOBS = 4                                         # একসাথে চলা জবের সীমা

ACCESS_CODE = os.getenv("ACCESS_CODE", "")           # সেট করলে অ্যাক্সেস কোড লাগবে
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")           # সেট করলে /api/admin/update চালু
UPDATE_HOURS = float(os.getenv("UPDATE_HOURS", "24"))  # কত ঘণ্টা পর পর yt-dlp আপডেট
AUTO_RESTART = os.getenv("AUTO_RESTART") == "1"

_hits, _jobs = {}, {}
_lock = threading.Lock()

# ---------- কুকি ব্যবস্থাপনা ----------
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

def get_extract_opts(url, cookie_path=None):
    opts = dict(BASE_OPTS)
    opts["format"] = "all"
    platform = detect_platform(url)
    if platform == "youtube":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "android_creator", "visionos", "android"]
            }
        }
    elif cookie_path:
        opts["cookiefile"] = cookie_path
    return opts

# ---------- ওয়ার্কার ফাংশন ----------
def run_video(job, url, quality):
    tmp = job["dir"]

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = min(90, int(d["downloaded_bytes"] * 90 / total))
            job["message"] = "ভিডিও ডাউনলোড হচ্ছে..."
        elif d["status"] == "finished":
            job["progress"] = 92
            job["message"] = "HD ভিডিও ও অডিও মার্জ হচ্ছে (FFmpeg)..."

    platform = detect_platform(url)
    opts = dict(BASE_OPTS,
                outtmpl=os.path.join(tmp, "%(title).80s.%(ext)s"),
                merge_output_format="mp4",
                progress_hooks=[hook],
                match_filter=yt_dlp.utils.match_filter_func(f"duration < {MAX_MINUTES * 60}"))

    if platform == "youtube":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "android_creator", "visionos", "android"]
            }
        }
    elif job.get("cookie"):
        opts["cookiefile"] = job.get("cookie")

    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320"
        }]
    else:
        q = int(re.sub(r"\D", "", quality) or 1080)
        # ফুল এইচডি বা এইচডির জন্য সর্বোচ্চ কোয়ালিটি নির্বাচন (ভার্টিক্যাল ১০৮০x১৩৫০ পর্যন্ত)
        max_h = 1440 if q >= 1080 else (900 if q >= 720 else q)
        opts["format"] = f"bv*[height<={max_h}]+ba/b[height<={max_h}]/b"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        # ফলব্যাক: ইউটিউবে কুকি ছাড়া ফেইল হলে কুকি ট্রাই করা
        if platform == "youtube" and job.get("cookie") and "cookiefile" not in opts:
            opts_fb = dict(opts, cookiefile=job["cookie"])
            opts_fb.pop("extractor_args", None)
            with yt_dlp.YoutubeDL(opts_fb) as ydl:
                ydl.extract_info(url, download=True)
        elif "cookiefile" in opts:
            opts_fb = dict(opts)
            opts_fb.pop("cookiefile", None)
            with yt_dlp.YoutubeDL(opts_fb) as ydl:
                ydl.extract_info(url, download=True)
        else:
            raise

    files = [os.path.join(tmp, f) for f in os.listdir(tmp)
             if os.path.isfile(os.path.join(tmp, f)) and not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError(f"ভিডিওটি ডাউনলোডযোগ্য নয় বা দৈর্ঘ্য {MAX_MINUTES} মিনিটের বেশি।")
    return files[0]

def run_images(job, url):
    tmp = job["dir"]
    job["message"] = "ফুল এইচডি ছবি খোঁজা ও ডাউনলোড হচ্ছে..."
    job["progress"] = 25

    cmd = [sys.executable, "-m", "gallery_dl", "-D", tmp, "--range", f"1-{MAX_IMAGES}"]
    if job.get("cookie"):
        cmd += ["--cookies", job["cookie"]]

    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=300)

    files = []
    for root, _, filenames in os.walk(tmp):
        for f in filenames:
            if not f.endswith((".zip", ".part", ".tmp")):
                files.append(os.path.join(root, f))
    files = sorted(files)
    if not files:
        err = (r.stderr or r.stdout or "")[-200:].strip()
        raise RuntimeError("কোনো ছবি পাওয়া যায়নি বা লিঙ্কটি প্রাইভেট। " + err)

    job["progress"] = 90
    if len(files) == 1:
        job["message"] = "ছবি প্রস্তুত!"
        return files[0]

    job["message"] = f"{len(files)}টি মূল এইচডি ছবির ZIP তৈরি হচ্ছে..."
    zpath = os.path.join(tmp, "HD_Images.zip")
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for fpath in files:
            z.write(fpath, os.path.basename(fpath))
    job["message"] = "প্রস্তুত!"
    return zpath

def worker(jid, url, mode, quality):
    job = _jobs[jid]
    try:
        path = run_images(job, url) if mode == "image" else run_video(job, url, quality)
        job.update(status="done", progress=100, message="ডাউনলোড প্রস্তুত!", path=path)
    except subprocess.TimeoutExpired:
        job.update(status="error", message="অনুরোধের সময়সীমা পার হয়ে গেছে।")
    except Exception as e:
        msg = str(e)
        if "Sign in" in msg:
            msg = "এই লিঙ্কটি ডাউনলোড করতে লগইন প্রয়োজন। অনুগ্রহ করে কুকি যোগ করুন।"
        job.update(status="error", message=msg[:250])
    finally:
        shutil.rmtree(job.get("cdir", ""), ignore_errors=True)

# ---------- এপিআই রুটস ----------
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/info")
def info():
    if not rate_ok(client_ip()):
        return jsonify(error="অতিরিক্ত অনুরোধ! কিছুক্ষণ পর আবার চেষ্টা করুন।"), 429
    if not check_access():
        return jsonify(error="প্রদত্ত অ্যাক্সেস কোডটি সঠিক নয়।"), 401

    url = (request.json or {}).get("url", "").strip()
    if not safe_url(url):
        return jsonify(error="অবৈধ বা অনিরাপদ লিঙ্ক প্রদান করা হয়েছে।"), 400

    platform = detect_platform(url)
    cdir = tempfile.mkdtemp(prefix="ck_")
    try:
        cf = cookie_copy(cdir)
        opts = get_extract_opts(url, cf)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                d = ydl.extract_info(url, download=False)
        except Exception as e:
            if platform == "youtube" and cf and "cookiefile" not in opts:
                opts_fb = dict(BASE_OPTS, cookiefile=cf, format="all")
                with yt_dlp.YoutubeDL(opts_fb) as ydl:
                    d = ydl.extract_info(url, download=False)
            elif "cookiefile" in opts:
                opts_fb = dict(opts)
                opts_fb.pop("cookiefile", None)
                with yt_dlp.YoutubeDL(opts_fb) as ydl:
                    d = ydl.extract_info(url, download=False)
            else:
                raise
    except Exception as e:
        err_str = str(e)
        if "Sign in" in err_str:
            err_str = "ভিডিওটি দেখতে লগইন প্রয়োজন (YouTube Bot Protection)।"
        return jsonify(error=f"তথ্য আনা যায়নি: {err_str[:200]}"), 422
    finally:
        shutil.rmtree(cdir, ignore_errors=True)

    if not d:
        return jsonify(error="কোনো মিডিয়া তথ্য পাওয়া যায়নি।"), 422

    if "entries" in d and d.get("entries"):
        d = next((e for e in d["entries"] if e), d)

    raw_formats = d.get("formats", [])
    # শুধুমাত্র রিয়েল ভিডিও স্ট্রিম থাকা রেজোলিউশন ফিল্টার (mhtml ও থাম্বনেইল স্টোরিবোর্ড বাদ দেওয়া)
    video_formats = [
        f for f in raw_formats
        if f.get("height")
        and f.get("vcodec") not in (None, "none", "images")
        and f.get("protocol") != "mhtml"
        and f.get("ext") != "mhtml"
        and not str(f.get("format_id", "")).startswith("sb")
        and int(f.get("height", 0)) >= 144
    ]
    heights = sorted({int(f["height"]) for f in video_formats}, reverse=True)

    # রেজোলিউশন ক্যাটাগরি তৈরি (1080p, 720p, 480p, 360p)
    resolutions = []
    seen = set()
    for h in heights:
        if h >= 1080 and "1080" not in seen:
            resolutions.append({"height": 1080, "actual_height": h, "label": "1080p", "badge": "Full HD"})
            seen.add("1080")
        elif h >= 720 and h < 1080 and "720" not in seen:
            resolutions.append({"height": 720, "actual_height": h, "label": "720p", "badge": "HD"})
            seen.add("720")
        elif h >= 480 and h < 720 and "480" not in seen:
            resolutions.append({"height": 480, "actual_height": h, "label": "480p", "badge": "SD"})
            seen.add("480")
        elif h >= 360 and h < 480 and "360" not in seen:
            resolutions.append({"height": 360, "actual_height": h, "label": "360p", "badge": "SD"})
            seen.add("360")

    # কোনো ভিডিও ফরম্যাট না পেলে ডিফল্ট স্ট্যান্ডার্ড অপশন
    if not resolutions:
        resolutions = [
            {"height": 1080, "actual_height": 1080, "label": "1080p", "badge": "Full HD"},
            {"height": 720, "actual_height": 720, "label": "720p", "badge": "HD"}
        ]

    return jsonify(
        platform=platform,
        title=d.get("title") or "মিডিয়া ফাইল",
        thumbnail=d.get("thumbnail") or "",
        uploader=d.get("uploader") or d.get("channel") or "",
        duration=d.get("duration") or 0,
        heights=heights,
        resolutions=resolutions,
        has_video=bool(heights),
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
    url = b.get("url", "").strip()
    mode = b.get("mode", "video")
    quality = str(b.get("quality", "1080"))

    if mode not in ("video", "image") or not safe_url(url):
        return jsonify(error="অনুরোধটি সঠিক নয়।"), 400

    with _lock:
        if sum(1 for j in _jobs.values() if j["status"] == "running") >= MAX_JOBS:
            return jsonify(error="সার্ভার বর্তমানে ব্যস্ত রয়েছে। অনুগ্রহ করে ১ মিনিট পর আবার চেষ্টা করুন।"), 503

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
        return jsonify(error="জব পাওয়া যায়নি বা মেয়াদ শেষ হয়ে গেছে।"), 404
    return jsonify(status=j["status"], progress=j["progress"], message=j["message"])

@app.get("/api/file/<jid>")
def file(jid):
    j = _jobs.get(jid)
    if not j or j["status"] != "done":
        return jsonify(error="ফাইল এখনও প্রস্তুত নয়।"), 404
    fpath = j["path"]
    filename = os.path.basename(fpath)
    return send_file(fpath, as_attachment=True, download_name=filename)

# ---------- সিস্টেম মেটাডাটা ও হেলথ ----------
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
    return jsonify(ok=True, build="v6-ultimate", versions=get_ver())

@app.get("/api/config")
def config():
    return jsonify(code_required=bool(ACCESS_CODE), max_minutes=MAX_MINUTES)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 7860)))
