"""Telegram মডিউল: নিজের অ্যাকাউন্টের লগইনে সেভ-অনুমোদিত চ্যাট থেকে মিডিয়া ডাউনলোড।

নীতি: 'protected content' (noforwards) চিহ্নিত চ্যাট/মেসেজ কখনো ডাউনলোড করা হয় না।
সেশন ও API কী শুধু এই ডিভাইসের DATA_DIR-এ থাকে (শুধু লোকাল মোডে চালু)।
"""
import os, re, json, asyncio, threading, zipfile, time, uuid, tempfile
from flask import jsonify, request, render_template

DATA_DIR = os.path.expanduser(os.getenv("DATA_DIR", "~/.media-downloader"))
CFG_PATH = os.path.join(DATA_DIR, "tg_config.json")
SESSION = os.path.join(DATA_DIR, "tg")
KINDS = ("photo", "video", "audio", "document")
SCAN_MAX = 3000
PROTECTED_MSG = "এই চ্যাটে কনটেন্ট সুরক্ষিত (protected content), তাই ডাউনলোড করা হবে না।"


# ---------- বিশুদ্ধ সহায়ক ফাংশন (টেস্টযোগ্য) ----------
def parse_link(text):
    m = re.search(r"(?:https?://)?t\.me/c/(\d+)/(?:\d+/)?(\d+)", text or "")
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})/(?:\d+/)?(\d+)", text or "")
    if m:
        return m.group(1), int(m.group(2))
    return None

def kind_of(msg):
    mt = type(getattr(msg, "media", None)).__name__
    if mt not in ("MessageMediaPhoto", "MessageMediaDocument"):
        return None                       # লিংক-প্রিভিউ ইত্যাদি বাদ
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "gif", None):
        return "video"
    if getattr(msg, "audio", None) or getattr(msg, "voice", None):
        return "audio"
    if getattr(msg, "document", None):
        return "document"
    return None

def load_cfg():
    if os.getenv("TG_API_ID") and os.getenv("TG_API_HASH"):
        return {"api_id": os.environ["TG_API_ID"], "api_hash": os.environ["TG_API_HASH"]}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("api_id") and d.get("api_hash") else None
    except Exception:
        return None

def save_cfg(api_id, api_hash):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump({"api_id": str(api_id), "api_hash": api_hash}, f)
    try:
        os.chmod(CFG_PATH, 0o600)
    except OSError:
        pass


# ---------- ডাউনলোড-লজিক (client ইনজেক্টযোগ্য, তাই টেস্টযোগ্য) ----------
async def run_download(client, job, target, kinds, limit, msg_id=None):
    entity = await client.get_entity(target)
    if getattr(entity, "noforwards", False):
        raise RuntimeError(PROTECTED_MSG)
    picked, skipped = [], 0
    if msg_id:
        m = await client.get_messages(entity, ids=msg_id)
        if not m or not kind_of(m):
            raise RuntimeError("এই লিংকে ডাউনলোডযোগ্য মিডিয়া নেই।")
        if getattr(m, "noforwards", False):
            raise RuntimeError(PROTECTED_MSG)
        picked = [m]
    else:
        job["message"] = "মেসেজ খোঁজা হচ্ছে..."
        async for m in client.iter_messages(entity, limit=SCAN_MAX):
            if kind_of(m) not in kinds:
                continue
            if getattr(m, "noforwards", False):
                skipped += 1
                continue
            picked.append(m)
            if len(picked) >= limit:
                break
    if not picked:
        extra = f" ({skipped}টি সুরক্ষিত মেসেজ বাদ গেছে)" if skipped else ""
        raise RuntimeError("কোনো ডাউনলোডযোগ্য মিডিয়া পাওয়া যায়নি।" + extra)

    n = len(picked)
    for i, m in enumerate(picked):
        job["message"] = f"ডাউনলোড {i + 1}/{n}" + (f" • {skipped}টি সুরক্ষিত বাদ" if skipped else "")
        def cb(cur, total, i=i):
            frac = (cur / total) if total else 0
            job["progress"] = min(95, int(3 + (i + frac) * 92 / n))
        await client.download_media(m, file=job["dir"] + os.sep, progress_callback=cb)

    files = sorted(f for f in os.listdir(job["dir"]) if os.path.isfile(os.path.join(job["dir"], f)))
    if not files:
        raise RuntimeError("ডাউনলোড ব্যর্থ হয়েছে।")
    if len(files) == 1:
        return os.path.join(job["dir"], files[0])
    job["message"] = "ZIP তৈরি হচ্ছে..."
    zp = os.path.join(job["dir"], "telegram_media.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(os.path.join(job["dir"], f), f)
    return zp


# ---------- Telethon ব্রিজ (আলাদা ইভেন্ট-লুপ থ্রেড) ----------
class Bridge:
    def __init__(self):
        self.loop = None
        self.client = None
        self._cfg = None
        self._lock = threading.Lock()
        self.pending = {}

    def run(self, coro, timeout=None):
        with self._lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def get_client(self):
        from telethon import TelegramClient
        cfg = load_cfg()
        if not cfg:
            raise RuntimeError("আগে API ID ও API Hash সেট করুন।")
        if self.client is None or self._cfg != cfg:
            if self.client:
                await self.client.disconnect()
            os.makedirs(DATA_DIR, exist_ok=True)
            self.client = TelegramClient(SESSION, int(cfg["api_id"]), cfg["api_hash"],
                                         connection_retries=2, request_retries=2, timeout=15)
            self._cfg = cfg
        if not self.client.is_connected():
            await self.client.connect()
        try:
            os.chmod(SESSION + ".session", 0o600)
        except OSError:
            pass
        return self.client

B = Bridge()


# ---------- রুট ----------
def init_telegram(app, jobs, lock, max_jobs, access_code, local, host):
    import hmac
    loopback = host in ("127.0.0.1", "localhost", "::1")
    enabled = bool(local and (loopback or access_code))
    reason = "" if enabled else ("শুধু লোকাল মোডে চালু থাকে।" if not local else
                                 "ফোন/নেটওয়ার্ক মোডে চালালে Telegram ব্যবহারের জন্য পাসওয়ার্ড (ACCESS_CODE) দিতে হবে।")

    def guard():
        if not enabled:
            return jsonify(error=reason), 403
        if access_code and not hmac.compare_digest(request.headers.get("X-Access-Code", ""), access_code):
            return jsonify(error="অ্যাক্সেস কোড ভুল।"), 401
        return None

    def fail(e):
        return jsonify(error=(str(e) or e.__class__.__name__)[:200]), 422

    @app.get("/telegram")
    def tg_page():
        return render_template("tg.html")

    @app.get("/api/tg/status")
    def tg_status():
        out = dict(enabled=enabled, reason=reason, code_required=bool(access_code))
        if guard() is not None:
            return jsonify(out)
        cfg = load_cfg()
        out.update(configured=bool(cfg), authorized=False)
        if cfg:
            async def f():
                c = await B.get_client()
                if await c.is_user_authorized():
                    me = await c.get_me()
                    return (me.first_name or me.username or "ব্যবহারকারী")
                return None
            try:
                name = B.run(f(), 30)
                out.update(authorized=bool(name), name=name)
            except Exception as e:
                out["error"] = "Telegram-এ সংযোগ হয়নি: " + (str(e) or e.__class__.__name__)[:120]
        return jsonify(out)

    @app.post("/api/tg/config")
    def tg_config():
        g = guard()
        if g is not None:
            return g
        b = request.json or {}
        try:
            api_id, api_hash = int(str(b.get("api_id", "")).strip()), str(b.get("api_hash", "")).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
                raise ValueError
        except ValueError:
            return jsonify(error="API ID সংখ্যা এবং API Hash ৩২ অক্ষরের হতে হবে।"), 400
        save_cfg(api_id, api_hash)
        return jsonify(ok=True)

    @app.post("/api/tg/send_code")
    def tg_send_code():
        g = guard()
        if g is not None:
            return g
        phone = str((request.json or {}).get("phone", "")).strip()
        async def f():
            c = await B.get_client()
            sent = await c.send_code_request(phone)
            B.pending[phone] = sent.phone_code_hash
        try:
            B.run(f(), 60)
            return jsonify(ok=True)
        except Exception as e:
            return fail(e)

    @app.post("/api/tg/login")
    def tg_login():
        g = guard()
        if g is not None:
            return g
        b = request.json or {}
        phone, code, pw = str(b.get("phone", "")).strip(), str(b.get("code", "")).strip(), b.get("password") or None
        async def f():
            from telethon.errors import SessionPasswordNeededError
            c = await B.get_client()
            try:
                if code:
                    await c.sign_in(phone, code, phone_code_hash=B.pending.get(phone))
            except SessionPasswordNeededError:
                if not pw:
                    return "need_password"
            if pw and not await c.is_user_authorized():
                await c.sign_in(password=pw)
            return "ok" if await c.is_user_authorized() else "failed"
        try:
            r = B.run(f(), 60)
            if r == "need_password":
                return jsonify(need_password=True)
            return jsonify(ok=(r == "ok")) if r == "ok" else (jsonify(error="লগইন ব্যর্থ।"), 422)
        except Exception as e:
            return fail(e)

    @app.post("/api/tg/logout")
    def tg_logout():
        g = guard()
        if g is not None:
            return g
        async def f():
            c = await B.get_client()
            await c.log_out()
        try:
            B.run(f(), 30)
        except Exception:
            pass
        B.client = None
        for ext in (".session", ".session-journal"):
            try:
                os.remove(SESSION + ext)
            except OSError:
                pass
        return jsonify(ok=True)

    @app.get("/api/tg/dialogs")
    def tg_dialogs():
        g = guard()
        if g is not None:
            return g
        async def f():
            c = await B.get_client()
            out = []
            async for d in c.iter_dialogs(limit=300):
                kind = "channel" if getattr(d, "is_channel", False) and not getattr(d, "is_group", False) else \
                       "group" if getattr(d, "is_group", False) else "user"
                out.append(dict(id=d.id, name=d.name or str(d.id), type=kind,
                                protected=bool(getattr(d.entity, "noforwards", False))))
            return out
        try:
            return jsonify(dialogs=B.run(f(), 90))
        except Exception as e:
            return fail(e)

    @app.post("/api/tg/start")
    def tg_start():
        g = guard()
        if g is not None:
            return g
        b = request.json or {}
        target, msg_id = None, None
        if b.get("link"):
            p = parse_link(str(b["link"]))
            if not p:
                return jsonify(error="অবৈধ Telegram মেসেজ লিংক।"), 400
            target, msg_id = p
        else:
            try:
                target = int(b.get("chat"))
            except (TypeError, ValueError):
                return jsonify(error="চ্যাট বেছে নিন।"), 400
        kinds = [k for k in b.get("kinds", []) if k in KINDS] or list(KINDS)
        limit = max(1, min(int(b.get("limit", 50) or 50), 500))
        with lock:
            if sum(1 for j in jobs.values() if j["status"] == "running") >= max_jobs:
                return jsonify(error="সার্ভার এখন ব্যস্ত।"), 503
            jid = uuid.uuid4().hex
            jobs[jid] = dict(status="running", progress=0, message="শুরু হচ্ছে...", created=time.time(),
                             dir=tempfile.mkdtemp(prefix="dl_"), cdir="")
        def worker():
            job = jobs[jid]
            async def f():
                c = await B.get_client()
                return await run_download(c, job, target, kinds, limit, msg_id)
            try:
                path = B.run(f())
                job.update(status="done", progress=100, message="প্রস্তুত!", path=path)
            except Exception as e:
                job.update(status="error", message=(str(e) or e.__class__.__name__)[:200])
        threading.Thread(target=worker, daemon=True).start()
        return jsonify(job=jid)

    return enabled
