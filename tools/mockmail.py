"""Tiny in-memory IMAP + SMTP server used only to test MailDesk offline.
IMAP on 127.0.0.1:1143, SMTP on 127.0.0.1:1025. Any password accepted.
Mail sent via SMTP to test@local is delivered into INBOX."""
import re, shlex, socketserver, threading, time, base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid

LOCK = threading.RLock()
BOX = {n: [] for n in ["INBOX", "Sent", "Drafts", "Trash", "Spam", "Archive", "Projects/Work"]}
NEXT = {n: 1 for n in BOX}
SPECIAL = {"Sent": "\\Sent", "Drafts": "\\Drafts", "Trash": "\\Trash", "Spam": "\\Junk", "Archive": "\\Archive"}
SMTP_LOG = []


def add(box, raw, flags=()):
    with LOCK:
        BOX[box].append({"uid": NEXT[box], "flags": set(flags), "raw": raw})
        NEXT[box] += 1


def seed():
    def mk(frm, subj, body, html=None, att=None, cid=None, date=None):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = frm, "Test User <test@local>", subj
        m["Date"] = date or formatdate(time.time() - 3600)
        m["Message-ID"] = make_msgid(domain="example.com")
        m.set_content(body)
        if html:
            m.add_alternative(html, subtype="html")
        if cid:
            m.get_payload()[1].add_related(cid, "image", "png", cid="<logo1>")
        if att:
            m.add_attachment(att, maintype="application", subtype="pdf", filename="report.pdf")
        return m.as_bytes()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    add("INBOX", mk("Alice <alice@example.com>", "Welcome to MailDesk", "Hi!\nVisit https://example.com today.\n\nBye"))
    add("INBOX", mk("Shop <news@shop.com>", "Big sale", "Sale!", html='<h2 style="color:red">Big Sale</h2><p>Hello <b>there</b></p><img src="https://tracker.example/pixel.gif"><script>alert(1)</script><img src="x" onerror="alert(2)">'))
    add("INBOX", mk("Bob <bob@example.com>", "Logo inline", "see html", html='<p>Logo:</p><img src="cid:logo1">', cid=png))
    add("INBOX", mk("Carol <carol@example.com>", "Invoice attached", "Please find the invoice attached.", att=b"%PDF-1.4 fake"), flags=["\\Seen", "\\Flagged"])
    add("INBOX", mk("Dëborah <deb@example.com>", "Ünïcode subject ✓ héllo", "Café déjà vu — 你好"), flags=["\\Seen"])
    for i in range(40):
        add("Archive", mk("Bulk <bulk@example.com>", "Archived message %d" % i, "body %d" % i), flags=["\\Seen"])


def utf7(s):
    return s


class IMAP(socketserver.StreamRequestHandler):
    def send(self, s):
        self.wfile.write(s if isinstance(s, bytes) else s.encode())
        self.wfile.flush()

    def handle(self):
        self.sel = None
        self.auth = False
        self.send("* OK mock IMAP ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            m = re.match(r"(\S+)\s+(\S+)\s*(.*)$", line)
            if not m:
                continue
            tag, cmd, rest = m.group(1), m.group(2).upper(), m.group(3)
            try:
                if self.dispatch(tag, cmd, rest) == "bye":
                    return
            except Exception as e:  # noqa
                import traceback; traceback.print_exc()
                self.send("%s BAD %s\r\n" % (tag, e))

    def dispatch(self, tag, cmd, rest):
        if cmd == "CAPABILITY":
            self.send("* CAPABILITY IMAP4rev1 UIDPLUS\r\n%s OK done\r\n" % tag)
        elif cmd == "NOOP":
            self.send("%s OK done\r\n" % tag)
        elif cmd == "LOGIN":
            self.auth = True
            self.send("%s OK [CAPABILITY IMAP4rev1 UIDPLUS] logged in\r\n" % tag)
        elif cmd == "LOGOUT":
            self.send("* BYE\r\n%s OK bye\r\n" % tag)
            return "bye"
        elif cmd == "LIST":
            out = ""
            for n in BOX:
                fl = SPECIAL.get(n, "")
                out += '* LIST (\\HasNoChildren %s) "/" "%s"\r\n' % (fl, n)
            out += '* LIST (\\Noselect \\HasChildren) "/" "[Gmail]"\r\n'
            self.send(out + "%s OK done\r\n" % tag)
        elif cmd == "STATUS":
            name = shlex.split(rest)[0]
            unseen = sum(1 for x in BOX[name] if "\\Seen" not in x["flags"])
            self.send('* STATUS "%s" (UNSEEN %d MESSAGES %d)\r\n%s OK done\r\n' % (name, unseen, len(BOX[name]), tag))
        elif cmd in ("SELECT", "EXAMINE"):
            name = shlex.split(rest)[0]
            if name not in BOX:
                return self.send("%s NO no such mailbox\r\n" % tag)
            self.sel = name
            self.send("* %d EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen \\Flagged \\Deleted \\Draft)\r\n%s OK [READ-WRITE] selected\r\n" % (len(BOX[name]), tag))
        elif cmd == "UID":
            sub, _, rest2 = rest.partition(" ")
            lm = re.search(r"\{(\d+)\}$", rest2)
            if lm:
                self.send("+ ready\r\n")
                lit = self.rfile.read(int(lm.group(1))).decode("utf-8")
                rest2 = rest2[:lm.start()] + '"' + lit.replace('"', '') + '"'
            self.uidcmd(tag, sub.upper(), rest2)
        elif cmd == "EXPUNGE":
            self.do_expunge(None)
            self.send("%s OK done\r\n" % tag)
        elif cmd == "APPEND":
            m = re.match(r'("(?:[^"\\]|\\.)*"|\S+)\s*(\([^)]*\))?\s*("[^"]*")?\s*\{(\d+)\}$', rest)
            name = shlex.split(m.group(1))[0]
            n = int(m.group(4))
            self.send("+ ready\r\n")
            data = self.rfile.read(n)
            self.rfile.readline()
            flags = set((m.group(2) or "()")[1:-1].split())
            add(name, data, flags)
            self.send("%s OK appended\r\n" % tag)
        else:
            self.send("%s BAD unknown %s\r\n" % (tag, cmd))

    def uids_from(self, spec):
        out = []
        box = BOX[self.sel]
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                hi = max(x["uid"] for x in box) if b == "*" else int(b)
                out += [x["uid"] for x in box if int(a) <= x["uid"] <= hi]
            else:
                out.append(int(part))
        return out

    def do_expunge(self, uids):
        with LOCK:
            BOX[self.sel][:] = [x for x in BOX[self.sel] if not ("\\Deleted" in x["flags"] and (uids is None or x["uid"] in uids))]

    def uidcmd(self, tag, sub, rest):
        box = BOX[self.sel]
        if sub == "SEARCH":
            toks = shlex.split(rest.replace("CHARSET UTF-8 ", ""))
            res = list(box)
            i = 0
            while i < len(toks):
                t = toks[i].upper()
                if t == "UNSEEN": res = [x for x in res if "\\Seen" not in x["flags"]]
                elif t == "FLAGGED": res = [x for x in res if "\\Flagged" in x["flags"]]
                elif t == "SEEN": res = [x for x in res if "\\Seen" in x["flags"]]
                elif t == "TEXT":
                    i += 1; needle = toks[i].lower().encode('utf-8')
                    res = [x for x in res if needle in x["raw"].lower()]
                i += 1
            self.send("* SEARCH %s\r\n%s OK done\r\n" % (" ".join(str(x["uid"]) for x in res), tag))
        elif sub == "FETCH":
            spec, _, items = rest.partition(" ")
            uids = self.uids_from(spec)
            out = b""
            for idx, x in enumerate(box):
                if x["uid"] not in uids:
                    continue
                parts = ["UID %d" % x["uid"], "FLAGS (%s)" % " ".join(sorted(x["flags"]))]
                lit = None
                if "RFC822.SIZE" in items:
                    parts.append("RFC822.SIZE %d" % len(x["raw"]))
                mm = re.search(r"BODY(?:\.PEEK)?\[HEADER\.FIELDS \(([^)]*)\)\]", items)
                if mm:
                    want = [w.lower() for w in mm.group(1).split()]
                    hdrs = x["raw"].split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
                    msg = BytesParser(policy=policy.compat32).parsebytes(hdrs + b"\r\n\r\n")
                    txt = "".join("%s: %s\r\n" % (k, str(v).replace("\n", "\r\n ").replace("\r\r", "\r")) for k, v in msg.items() if k.lower() in want) + "\r\n"
                    # re-emit raw header lines to keep encoded words
                    raw_lines = []
                    cur = b""
                    for ln in hdrs.splitlines(keepends=True):
                        if ln[:1] in b" \t" and cur:
                            cur += ln
                        else:
                            if cur: raw_lines.append(cur)
                            cur = ln
                    if cur: raw_lines.append(cur)
                    sel = b"".join(l if l.endswith(b"\n") else l + b"\r\n" for l in raw_lines if l.split(b":", 1)[0].decode().lower() in want) + b"\r\n"
                    lit = ("BODY[HEADER.FIELDS (%s)]" % mm.group(1), sel)
                elif re.search(r"BODY(?:\.PEEK)?\[\]", items):
                    lit = ("BODY[]", x["raw"])
                head = ("* %d FETCH (%s" % (idx + 1, " ".join(parts))).encode()
                if lit:
                    out += head + (" %s {%d}\r\n" % (lit[0], len(lit[1]))).encode() + lit[1] + b")\r\n"
                else:
                    out += head + b")\r\n"
                if "BODY.PEEK" not in items and "BODY[" in items:
                    x["flags"].add("\\Seen")
            self.send(out + ("%s OK done\r\n" % tag).encode())
        elif sub == "STORE":
            spec, op, fl = rest.split(" ", 2)
            flags = set(fl.strip("()").split())
            for x in box:
                if x["uid"] in self.uids_from(spec):
                    if op == "+FLAGS": x["flags"] |= flags
                    elif op == "-FLAGS": x["flags"] -= flags
            self.send("%s OK stored\r\n" % tag)
        elif sub == "COPY":
            spec, dest = rest.split(" ", 1)
            dest = shlex.split(dest)[0]
            ids = self.uids_from(spec)
            for x in list(box):
                if x["uid"] in ids:
                    add(dest, x["raw"], x["flags"] - {"\\Deleted"})
            self.send("%s OK copied\r\n" % tag)
        elif sub == "EXPUNGE":
            self.do_expunge(set(self.uids_from(rest)))
            self.send("%s OK done\r\n" % tag)
        else:
            self.send("%s BAD uid %s\r\n" % (tag, sub))


class SMTP(socketserver.StreamRequestHandler):
    def send(self, s):
        self.wfile.write(s.encode()); self.wfile.flush()

    def handle(self):
        self.send("220 mock SMTP\r\n")
        rcpts = []
        while True:
            line = self.rfile.readline()
            if not line: return
            c = line.decode().strip()
            u = c.upper()
            if u.startswith("EHLO") or u.startswith("HELO"):
                self.send("250-mock\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif u.startswith("AUTH"):
                self.send("235 ok\r\n")
            elif u.startswith("MAIL"):
                rcpts = []; self.send("250 ok\r\n")
            elif u.startswith("RCPT"):
                rcpts.append(re.search(r"<([^>]*)>", c).group(1)); self.send("250 ok\r\n")
            elif u == "DATA":
                self.send("354 go\r\n")
                buf = b""
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b".\n"): break
                    buf += l[1:] if l.startswith(b"..") else l
                SMTP_LOG.append((rcpts[:], buf))
                if "test@local" in rcpts:
                    add("INBOX", buf)
                self.send("250 queued\r\n")
            elif u == "QUIT":
                self.send("221 bye\r\n"); return
            elif u == "RSET":
                self.send("250 ok\r\n")
            else:
                self.send("250 ok\r\n")


class TS(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    seed()
    threading.Thread(target=TS(("127.0.0.1", 1143), IMAP).serve_forever, daemon=True).start()
    threading.Thread(target=TS(("127.0.0.1", 1025), SMTP).serve_forever, daemon=True).start()
    print("mock mail up: IMAP 1143, SMTP 1025", flush=True)
    while True:
        time.sleep(3600)
