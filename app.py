"""
MailDesk - a self-hosted webmail client.

Log in with ANY mailbox that supports IMAP + SMTP (Gmail, Outlook, Yahoo, iCloud,
Zoho, your own domain...) and use it through a Gmail-style web UI.

Run:   pip install flask
       python app.py
Open:  http://127.0.0.1:5000

Passwords are kept only in server memory for the life of the login session
(never written to disk).
"""
import base64
import email
import functools
import imaplib
import json
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import ssl
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import (formataddr, formatdate, getaddresses, make_msgid,
                         parseaddr, parsedate_to_datetime)

from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB per request (attachments)

PAGE_SIZE = 30
SESSION_TTL = 12 * 3600
COOKIE = "maildesk_sid"
SESSIONS = {}  # sid -> {"cfg": {...}, "seen": ts}

# Hosts that already file a copy of sent mail themselves (don't APPEND to Sent twice)
AUTO_SAVE_SENT = ("gmail.com", "googlemail.com", "office365.com", "outlook.com", "yahoo.com", "zoho")


# --------------------------------------------------------------------------- utils
def utf7_decode(s: str) -> str:
    def repl(m):
        if m.group(1) == "":
            return "&"
        b = m.group(1).replace(",", "/")
        b += "=" * (-len(b) % 4)
        return base64.b64decode(b).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, s)


def utf7_encode(s: str) -> str:
    out, buf = [], ""

    def flush():
        nonlocal buf
        if buf:
            b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf = ""

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def q(name: str) -> str:
    """Quote a (decoded) mailbox name for use in an IMAP command."""
    enc = utf7_encode(name)
    return '"' + enc.replace("\\", "\\\\").replace('"', '\\"') + '"'


def api_error(msg, code=400):
    return jsonify({"error": str(msg)}), code


def get_session():
    sid = request.cookies.get(COOKIE)
    s = SESSIONS.get(sid)
    if not s:
        return None, None
    if time.time() - s["seen"] > SESSION_TTL:
        SESSIONS.pop(sid, None)
        return None, None
    s["seen"] = time.time()
    return sid, s


def auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        sid, s = get_session()
        if not s:
            return api_error("Not signed in", 401)
        try:
            return fn(s["cfg"], *a, **kw)
        except imaplib.IMAP4.abort as e:
            return api_error("Connection to mail server dropped: %s" % e, 502)
        except imaplib.IMAP4.error as e:
            return api_error("Mail server error: %s" % e, 400)
        except smtplib.SMTPAuthenticationError as e:
            return api_error("SMTP login failed: %s" % (e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else e), 401)
        except smtplib.SMTPException as e:
            return api_error("SMTP error: %s" % e, 502)
        except (socket.timeout, TimeoutError):
            return api_error("Mail server timed out", 504)
        except (OSError, ssl.SSLError) as e:
            return api_error("Could not reach mail server: %s" % e, 502)
    return wrapper


def imap_connect(cfg):
    host, port = cfg["imap_host"], int(cfg["imap_port"])
    sec = cfg.get("imap_security", "ssl")
    ctx = ssl.create_default_context()
    if sec == "ssl":
        m = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
    else:
        m = imaplib.IMAP4(host, port, timeout=30)
        if sec == "starttls":
            m.starttls(ssl_context=ctx)
    m.login(cfg["imap_user"], cfg["password"])
    return m


class Imap:
    """Context manager: connect, optionally select a folder, always logout."""

    def __init__(self, cfg, folder=None, readonly=False):
        self.cfg, self.folder, self.readonly = cfg, folder, readonly

    def __enter__(self):
        self.m = imap_connect(self.cfg)
        if self.folder:
            typ, data = self.m.select(q(self.folder), readonly=self.readonly)
            if typ != "OK":
                self.m.logout()
                raise imaplib.IMAP4.error("Cannot open folder %r: %s" % (self.folder, data))
        return self.m

    def __exit__(self, *exc):
        try:
            self.m.logout()
        except Exception:
            pass


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.+)$', re.S)

ROLE_FLAGS = {"\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash", "\\junk": "spam",
              "\\archive": "archive", "\\all": "all", "\\flagged": "starred", "\\important": "important"}
ROLE_NAMES = {
    "sent": ("sent", "sent items", "sent messages", "sent mail", "[gmail]/sent mail"),
    "drafts": ("drafts", "draft", "[gmail]/drafts"),
    "trash": ("trash", "deleted", "deleted items", "deleted messages", "bin", "[gmail]/trash", "[gmail]/bin"),
    "spam": ("spam", "junk", "junk e-mail", "junk email", "bulk mail", "[gmail]/spam"),
    "archive": ("archive", "archives", "[gmail]/all mail"),
}


def list_folders(m):
    typ, data = m.list()
    folders = []
    for item in data or []:
        if isinstance(item, tuple):
            line = item[0].decode("utf-8", "replace")
            flags = re.search(r"\(([^)]*)\)", line).group(1)
            delim = re.search(r'\)\s+(NIL|"[^"]*")', line).group(1).strip('"')
            raw = item[1].decode("utf-8", "replace")
        elif item:
            line = item.decode("utf-8", "replace")
            mm = LIST_RE.search(line)
            if not mm:
                continue
            flags, delim, raw = mm.group("flags"), mm.group("delim").strip('"'), mm.group("name").strip()
            if raw.startswith('"') and raw.endswith('"'):
                raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
        else:
            continue
        name = utf7_decode(raw)
        fl = [f.lower() for f in flags.split()]
        if "\\noselect" in fl or "\\nonexistent" in fl:
            continue
        role = None
        for f in fl:
            if f in ROLE_FLAGS:
                role = ROLE_FLAGS[f]
        if name.upper() == "INBOX":
            role = "inbox"
        if not role:
            low = name.lower()
            for r, names in ROLE_NAMES.items():
                if low in names:
                    role = r
        folders.append({"name": name, "role": role, "delim": delim if delim != "NIL" else "/"})
    return folders


def folder_by_role(m, role):
    for f in list_folders(m):
        if f["role"] == role:
            return f["name"]
    return None


def parse_flags(text):
    mm = re.search(r"FLAGS \(([^)]*)\)", text)
    return [f.lower() for f in mm.group(1).split()] if mm else []


def iter_fetch(data):
    """Yield (uid, flags, payload_bytes) from an imaplib FETCH response."""
    cur = None
    for item in data:
        if isinstance(item, tuple):
            if cur:
                yield cur
            meta = item[0].decode("latin-1")
            uid = re.search(r"UID (\d+)", meta)
            cur = {"uid": uid.group(1) if uid else None, "flags": parse_flags(meta), "payload": item[1]}
        elif isinstance(item, (bytes, bytearray)) and cur:
            tail = item.decode("latin-1")
            if not cur["uid"]:
                uid = re.search(r"UID (\d+)", tail)
                cur["uid"] = uid.group(1) if uid else None
            if not cur["flags"]:
                cur["flags"] = parse_flags(tail)
    if cur:
        yield cur


def hdr(msg, name):
    try:
        v = msg[name]
        return str(v) if v is not None else ""
    except Exception:
        return str(msg.get(name, ""))


def addr_list(value):
    out = []
    for name, addr in getaddresses([value]):
        if addr or name:
            out.append({"name": name, "addr": addr})
    return out


def date_ts(value):
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except Exception:
        return 0


def safe_content(part):
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")


# --------------------------------------------------------------------------- routes: static
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.after_request
def secure(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- routes: session
@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(silent=True) or {}
    need = ("email", "password", "imap_host", "imap_port", "smtp_host", "smtp_port")
    if any(not str(d.get(k, "")).strip() for k in need):
        return api_error("Please fill in email, password and the IMAP/SMTP server settings.")
    cfg = {
        "email": d["email"].strip(),
        "name": (d.get("name") or "").strip(),
        "password": d["password"],
        "imap_host": d["imap_host"].strip(),
        "imap_port": int(d["imap_port"]),
        "imap_security": d.get("imap_security", "ssl"),
        "imap_user": (d.get("imap_user") or d["email"]).strip(),
        "smtp_host": d["smtp_host"].strip(),
        "smtp_port": int(d["smtp_port"]),
        "smtp_security": d.get("smtp_security", "starttls"),
        "smtp_user": (d.get("smtp_user") or d.get("imap_user") or d["email"]).strip(),
    }
    try:
        m = imap_connect(cfg)
        m.logout()
    except imaplib.IMAP4.error as e:
        return api_error("Login rejected by the mail server: %s. (Gmail/Yahoo/iCloud need an app password, not your normal password.)" % e, 401)
    except (OSError, ssl.SSLError) as e:
        return api_error("Could not connect to %s:%s - %s" % (cfg["imap_host"], cfg["imap_port"], e), 502)

    for k in [k for k, v in SESSIONS.items() if time.time() - v["seen"] > SESSION_TTL]:
        SESSIONS.pop(k, None)
    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = {"cfg": cfg, "seen": time.time()}
    resp = jsonify({"ok": True, "email": cfg["email"], "name": cfg["name"]})
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Strict", max_age=SESSION_TTL)
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    sid, _ = get_session()
    SESSIONS.pop(sid, None)
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.route("/api/me")
def me():
    _, s = get_session()
    if not s:
        return api_error("Not signed in", 401)
    return jsonify({"email": s["cfg"]["email"], "name": s["cfg"]["name"]})


# --------------------------------------------------------------------------- routes: mail
@app.route("/api/folders")
@auth
def folders(cfg):
    with Imap(cfg) as m:
        fl = list_folders(m)
        for f in fl:
            try:
                typ, data = m.status(q(f["name"]), "(UNSEEN)")
                mm = re.search(r"UNSEEN (\d+)", data[0].decode("latin-1")) if typ == "OK" and data and data[0] else None
                f["unread"] = int(mm.group(1)) if mm else 0
            except Exception:
                f["unread"] = 0
    order = {"inbox": 0, "starred": 1, "sent": 2, "drafts": 3, "archive": 4, "all": 5, "spam": 6, "trash": 7}
    fl.sort(key=lambda f: (order.get(f["role"], 10), f["name"].lower()))
    return jsonify({"folders": fl})


@app.route("/api/messages")
@auth
def messages(cfg):
    folder = request.args.get("folder", "INBOX")
    page = max(1, int(request.args.get("page", 1)))
    query = (request.args.get("q") or "").strip()
    flt = request.args.get("filter", "")  # "unread" | "starred"
    with Imap(cfg, folder, readonly=True) as m:
        crit = []
        if flt == "unread":
            crit.append("UNSEEN")
        elif flt == "starred":
            crit.append("FLAGGED")
        if not crit and not query:
            crit.append("ALL")
        if query:
            try:
                query.encode("ascii")
                typ, data = m.uid("SEARCH", None, *crit, "TEXT", '"%s"' % query.replace("\\", "\\\\").replace('"', '\\"'))
            except UnicodeEncodeError:
                m.literal = query.encode("utf-8")
                typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", *crit, "TEXT")
        else:
            typ, data = m.uid("SEARCH", None, *crit)
        uids = data[0].split() if data and data[0] else []
        uids = [u.decode() for u in uids][::-1]  # newest first
        total = len(uids)
        chunk = uids[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        out = []
        if chunk:
            typ, data = m.uid("FETCH", ",".join(chunk),
                              "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE CONTENT-TYPE)])")
            by_uid = {}
            for r in iter_fetch(data):
                h = email.message_from_bytes(r["payload"], policy=policy.default)
                frm = parseaddr(hdr(h, "from"))
                ts = date_ts(hdr(h, "date"))
                by_uid[r["uid"]] = {
                    "uid": r["uid"],
                    "from_name": frm[0] or frm[1],
                    "from_addr": frm[1],
                    "to": hdr(h, "to"),
                    "subject": hdr(h, "subject") or "(no subject)",
                    "ts": ts,
                    "seen": "\\seen" in r["flags"],
                    "starred": "\\flagged" in r["flags"],
                    "draft": "\\draft" in r["flags"],
                    "attach": "multipart/mixed" in hdr(h, "content-type").lower(),
                }
            out = [by_uid[u] for u in chunk if u in by_uid]
    return jsonify({"messages": out, "total": total, "page": page, "page_size": PAGE_SIZE})


def fetch_full(m, uid):
    typ, data = m.uid("FETCH", uid, "(UID FLAGS BODY.PEEK[])")
    rows = list(iter_fetch(data or []))
    if not rows or not rows[0]["payload"]:
        return None, []
    return email.message_from_bytes(rows[0]["payload"], policy=policy.default), rows[0]["flags"]


def leaves_of(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


@app.route("/api/message")
@auth
def message(cfg):
    folder, uid = request.args["folder"], request.args["uid"]
    mark = request.args.get("mark_read", "1") == "1"
    with Imap(cfg, folder, readonly=False) as m:
        msg, flags = fetch_full(m, uid)
        if msg is None:
            return api_error("Message not found", 404)
        if mark and "\\seen" not in flags:
            m.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            flags.append("\\seen")

    body = msg.get_body(preferencelist=("html", "plain"))
    plain = msg.get_body(preferencelist=("plain",))
    html = text = None
    if body is not None:
        content = safe_content(body)
        if body.get_content_subtype() == "html":
            html = content
        else:
            text = content
    if plain is not None and text is None:
        text = safe_content(plain)

    leaves, atts, cids = leaves_of(msg), [], {}
    for i, p in enumerate(leaves):
        if p is body or p is plain:
            continue
        fn = p.get_filename()
        cid = (p.get("Content-ID") or "").strip("<> \r\n\t")
        disp = (p.get_content_disposition() or "")
        if p.get_content_maintype() == "text" and not fn and disp != "attachment":
            continue
        size = len(p.get_payload(decode=True) or b"")
        if cid:
            cids[cid] = i
        atts.append({"idx": i, "filename": fn or ("inline-%d" % i), "type": p.get_content_type(),
                     "size": size, "inline": bool(cid) and disp != "attachment"})

    if html and cids:
        def sub(mo):
            idx = cids.get(mo.group(1))
            if idx is None:
                return mo.group(0)
            return "/api/attachment?folder=%s&uid=%s&idx=%d&inline=1" % (
                __import__("urllib.parse").parse.quote(folder, safe=""), uid, idx)
        html = re.sub(r"cid:([^\"'\s)>]+)", sub, html, flags=re.I)

    return jsonify({
        "uid": uid, "folder": folder,
        "subject": hdr(msg, "subject") or "(no subject)",
        "from": addr_list(hdr(msg, "from")),
        "to": addr_list(hdr(msg, "to")),
        "cc": addr_list(hdr(msg, "cc")),
        "reply_to": addr_list(hdr(msg, "reply-to")),
        "ts": date_ts(hdr(msg, "date")),
        "message_id": hdr(msg, "message-id").strip(),
        "references": hdr(msg, "references").strip(),
        "html": html, "text": text,
        "attachments": [a for a in atts if not a["inline"]],
        "seen": "\\seen" in flags, "starred": "\\flagged" in flags,
    })


@app.route("/api/attachment")
@auth
def attachment(cfg):
    folder, uid, idx = request.args["folder"], request.args["uid"], int(request.args["idx"])
    with Imap(cfg, folder, readonly=True) as m:
        msg, _ = fetch_full(m, uid)
    if msg is None:
        return api_error("Message not found", 404)
    leaves = leaves_of(msg)
    if idx >= len(leaves):
        return api_error("Attachment not found", 404)
    p = leaves[idx]
    data = p.get_payload(decode=True) or b""
    ctype = p.get_content_type()
    inline = request.args.get("inline") == "1" and ctype.startswith("image/") and "svg" not in ctype
    fn = p.get_filename() or "attachment-%d" % idx
    resp = Response(data, mimetype=ctype if inline else "application/octet-stream")
    safe = fn.replace('"', "").replace("\r", "").replace("\n", "")
    from urllib.parse import quote
    resp.headers["Content-Disposition"] = "%s; filename*=UTF-8''%s" % ("inline" if inline else "attachment", quote(safe))
    return resp


@app.route("/api/action", methods=["POST"])
@auth
def action(cfg):
    d = request.get_json(silent=True) or {}
    folder, uids, act = d.get("folder"), d.get("uids") or [], d.get("action")
    if not folder or not uids:
        return api_error("Missing folder or messages")
    ids = ",".join(str(int(u)) for u in uids)
    with Imap(cfg, folder) as m:
        if act in ("read", "unread", "star", "unstar"):
            op = "+FLAGS" if act in ("read", "star") else "-FLAGS"
            flag = "(\\Seen)" if act in ("read", "unread") else "(\\Flagged)"
            m.uid("STORE", ids, op, flag)
        elif act == "delete":
            trash = folder_by_role(m, "trash")
            if trash and trash != folder:
                move_uids(m, ids, trash)
            else:
                m.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
                expunge(m, ids)
        elif act == "spam":
            dest = folder_by_role(m, "spam")
            if not dest:
                return api_error("No spam/junk folder found on this account")
            move_uids(m, ids, dest)
        elif act in ("archive", "move"):
            dest = d.get("target") if act == "move" else folder_by_role(m, "archive")
            if not dest:
                return api_error("No archive folder found on this account")
            move_uids(m, ids, dest)
        else:
            return api_error("Unknown action")
    return jsonify({"ok": True})


def expunge(m, ids):
    caps = " ".join(getattr(m, "capabilities", ())).upper()
    if "UIDPLUS" in caps:
        m.uid("EXPUNGE", ids)
    else:
        m.expunge()


def move_uids(m, ids, dest):
    caps = " ".join(getattr(m, "capabilities", ())).upper()
    if " MOVE" in " " + caps:
        typ, data = m.uid("MOVE", ids, q(dest))
        if typ == "OK":
            return
    typ, data = m.uid("COPY", ids, q(dest))
    if typ != "OK":
        raise imaplib.IMAP4.error("Could not copy to %s: %s" % (dest, data))
    m.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
    expunge(m, ids)


def build_message(cfg, p, files):
    to = getaddresses([p.get("to", "").replace(";", ",")])
    cc = getaddresses([p.get("cc", "").replace(";", ",")])
    bcc = getaddresses([p.get("bcc", "").replace(";", ",")])
    rcpts = [a for _, a in to + cc + bcc if a]
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["name"], cfg["email"])) if cfg["name"] else cfg["email"]
    if to:
        msg["To"] = ", ".join(formataddr(x) for x in to if x[1])
    if cc:
        msg["Cc"] = ", ".join(formataddr(x) for x in cc if x[1])
    if bcc:
        msg["Bcc"] = ", ".join(formataddr(x) for x in bcc if x[1])
    msg["Subject"] = p.get("subject", "")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["email"].split("@")[-1])
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = p["in_reply_to"]
        msg["References"] = (p.get("references", "") + " " + p["in_reply_to"]).strip()
    text = p.get("text", "")
    html = p.get("html", "")
    msg.set_content(text or " ")
    if html:
        msg.add_alternative(html, subtype="html")
    for f in files:
        data = f.read()
        ctype = f.mimetype or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=f.filename)
    return msg, rcpts


@app.route("/api/send", methods=["POST"])
@auth
def send(cfg):
    p = json.loads(request.form.get("payload", "{}"))
    msg, rcpts = build_message(cfg, p, request.files.getlist("attachments"))
    if not rcpts:
        return api_error("Add at least one recipient.")
    sec, host, port = cfg["smtp_security"], cfg["smtp_host"], cfg["smtp_port"]
    ctx = ssl.create_default_context()
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.ehlo()
        if sec == "starttls":
            s.starttls(context=ctx)
            s.ehlo()
    try:
        if cfg["password"]:
            s.login(cfg["smtp_user"], cfg["password"])
        s.send_message(msg, from_addr=cfg["email"], to_addrs=rcpts)
    finally:
        try:
            s.quit()
        except Exception:
            pass

    saved = False
    if not any(h in cfg["imap_host"].lower() for h in AUTO_SAVE_SENT):
        try:
            with Imap(cfg) as m:
                sent = folder_by_role(m, "sent")
                if sent:
                    m.append(q(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                    saved = True
        except Exception:
            pass
    if p.get("draft_uid") and p.get("draft_folder"):
        try:
            with Imap(cfg, p["draft_folder"]) as m:
                m.uid("STORE", str(int(p["draft_uid"])), "+FLAGS", "(\\Deleted)")
                expunge(m, str(int(p["draft_uid"])))
        except Exception:
            pass
    return jsonify({"ok": True, "saved_to_sent": saved})


@app.route("/api/draft", methods=["POST"])
@auth
def draft(cfg):
    p = json.loads(request.form.get("payload", "{}"))
    msg, _ = build_message(cfg, p, request.files.getlist("attachments"))
    with Imap(cfg) as m:
        folder = folder_by_role(m, "drafts")
        if not folder:
            return api_error("No Drafts folder found on this account")
        m.append(q(folder), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if p.get("draft_uid") and p.get("draft_folder"):
            m.select(q(p["draft_folder"]))
            m.uid("STORE", str(int(p["draft_uid"])), "+FLAGS", "(\\Deleted)")
            expunge(m, str(int(p["draft_uid"])))
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print("MailDesk running on http://%s:%d" % (host, port))
    app.run(host=host, port=port, threaded=True)
