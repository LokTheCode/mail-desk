# MailDesk – Gmail-style webmail for any email account

Sign in with any mailbox that supports **IMAP + SMTP** and use it through a Gmail-like web UI.
This is a mail *client* (like Roundcube / Gmail's web app); it connects to your existing
mail provider. It is not a mail server that receives mail from the internet itself (see bottom).

## Run (Windows / Mac / Linux)

```
pip install flask
python app.py
```
Open http://127.0.0.1:5000 and sign in. No database, no other dependencies, free.

## Signing in

Pick a provider chip (Gmail, Outlook, Yahoo, iCloud, Zoho) or "Other" and edit the server
settings. Most providers require an **app password**, not your normal password:

| Provider | What you need |
|---|---|
| Gmail | Turn on 2-Step Verification, then create an App Password (Google Account → Security) |
| Yahoo | App password (Account Security) |
| iCloud | App-specific password (appleid.apple.com) |
| Zoho | Enable IMAP in settings; app password if 2FA is on |
| Outlook / Microsoft 365 | Microsoft is moving to OAuth sign-in; password login may be refused depending on account type. Not verified here. |
| Own domain / hosting | Use the IMAP/SMTP names from your host (often `mail.yourdomain.com`) |

## Features
- Folders (Inbox, Sent, Drafts, Archive, Spam, Trash, custom) with unread counts
- Read, star, mark read/unread, archive, spam, delete, move to folder, bulk select
- Search (server-side), Unread/Starred filters, paging
- Compose, reply, reply all, forward, Cc/Bcc, rich text, attachments, save/reopen drafts
- HTML email rendered in a sandbox: scripts blocked, remote images hidden until you click "Display images"
- Inline images and attachment downloads, dark mode, mobile-friendly layout, auto-refresh

## Security notes
- Your password lives only in server memory for the session (12 h), never on disk.
- By default it listens on 127.0.0.1 only. If you host it for others, put it behind HTTPS
  (nginx/Caddy) and run with a real WSGI server (e.g. `waitress-serve --port=5000 app:app`).
  Set `HOST=0.0.0.0` only after doing that.
- Sessions are in memory: restarting the server signs everyone out.

## Known limits
- Search uses standard IMAP `TEXT` search (not Gmail's special query syntax).
- No push/IDLE: the list refreshes every 60 s and on the refresh button.
- Reopened drafts don't carry their old attachments; no conversation threading yet.
- Tested against a local mock IMAP/SMTP server (`tools/mockmail.py`), not live Gmail/Outlook.
  Try it offline: `python tools/mockmail.py`, then sign in as `test@local`, any password,
  IMAP `127.0.0.1:1143` security None, SMTP `127.0.0.1:1025` security None.

## Want a real mail server instead?
Receiving mail for your own domain needs a server stack (Postfix + Dovecot, or an all-in-one
like Mailu / Mail-in-a-Box), a domain with MX/SPF/DKIM/DMARC records and a host that allows port 25.
MailDesk works as the web UI on top of any of those too: just point it at their IMAP/SMTP ports.
