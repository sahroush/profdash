#!/usr/bin/env python3
"""
Gmail Label Applications — Cron Script
Step 1 labels sent application emails. Step 2 classifies each labeled thread
into exactly one status sub-label (deterministic, no model calls).
"""

import base64
import html as htmlmod
import json
import os
import re
import sqlite3 as _sq
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

HERMES_VENV_PYTHON = Path(os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python3"))
TOKEN_PATH = os.path.expanduser("~/.hermes/google_token.json")

DRY_RUN = os.environ.get("GMAIL_LABEL_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")

# --- Paths / Configuration ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'evaluations.sqlite'

UTC = timezone.utc
CYCLE_START = datetime(2026, 1, 1, tzinfo=UTC)
WAIT_DAYS = 5
NO_REPLY_NUDGES = 5
# Your own Gmail address/addresses — used to skip self-sent mail and
# never treat your own outgoing messages as professor replies.
# Override with the PROF_MY_ADDRESSES env var (comma-separated).
MY_ADDRESSES = {
    a.strip() for a in os.environ.get(
        "PROF_MY_ADDRESSES", "you@example.com").split(",") if a.strip()
}
WAIT_HOURS = WAIT_DAYS * 24
_MIN_REQ_INTERVAL = 0.21  # ~5 Gmail requests/sec

# Label names resolved from the dashboard DB config (2026-09-07): Gmail labels
# were renamed to "2026 applications" + "status-*" sub-labels (with
# status-processing). DB config is the source of truth; falls back to the
# 2026-applications convention when the DB is unreadable.

def _load_label_config():
    base, status = '2026 applications', {}
    try:
        conn = _sq.connect(str(DB_PATH), timeout=5)
        for k, v in conn.execute("SELECT key, value FROM config WHERE key LIKE 'label_%'"):
            if k == 'label_base':
                base = v or base
            elif k.startswith('label_'):
                status[k[len('label_'):]] = v
        conn.close()
    except Exception:
        pass
    if not status:
        status = {
            'accepted': 'status-accepted',
            'rejected': 'status-rejected',
            'go_apply': 'status-go-apply',
            'conditional': 'status-conditional',
            'follow_up': 'status-follow-up',
            'no_reply': 'status-no-reply',
            'processing': 'status-processing',
            'ongoing': 'status-ongoing',
        }
    # Sub-labels live under the base label in Gmail; bare names mean exactly that.
    status = {
        (k if '/' in v else v.split('/')[-1] if False else k): (v if '/' in v else f"{base}/{v}")
        for k, v in status.items()
    }
    return base, status

LABEL_BASE, LABELS_STATUS = _load_label_config()

# --- Filters ---
# NOTE: bare 'professor' removed (2026-08-28) — "Dear Professor X" in
# research-collaboration/meeting-scheduling replies caused repeated false
# positives (patrol-scheduling thread with the NTU internship supervisors).
ACADEMIC_KEYWORDS = [
    'phd', 'master', 'mphil', 'msc', 'meng', 'postdoc', 'supervision',
    'research group', 'opening', 'position', 'prospective'
]

# Strong application-intent keywords — required for Re:/Fwd: replies whose
# thread does NOT already carry the base label (see run(), step 1).
# 2026-10-16: degree tokens (msc/mphil/meng/...) in the SUBJECT are intent
# enough (Khu "Re: Meeting for MSc VinUni" — follow-up split into a fresh
# thread by the sender's client; only the subject carries the context).
STRONG_APPLICATION_KW = [
    'prospective', 'apply', 'application', 'admission', 'opening',
    'supervision', 'opportunities in your group', "master's with you",
    'phd position', 'join your group', 'interested in your research',
]

EXCLUDE_PATTERNS = [
    'work exchange', 'summer exchange', 'internship exchange',
    'work/project exchange', 'work-exchange',
    'housing', 'hostel', 'room', 'accommodation', 'rent',
    'mayrooms', 'homey', 'dormitor', 'sublet', 'airbnb',
    'visa processing', 'visa application', 'visa timeline',
    'deferral', 'deferral inquiry', 'immigration',
    'mailing list', 'newsletter', 'unsubscribe',
    'automated', 'no-reply', 'noreply',
]

# --- Classification Patterns ---
REJECTED_PATTERNS = [
    r'\bnot accepting (?:new )?(?:phd |master[\'’]?s? )?students?\b',
    r'\bno (?:funded )?(?:phd |postdoc )?(?:positions?|openings?) (?:are )?available\b',
    r'\b(?:do not|don[\'’]?t|cannot|can[\'’]?t|am not|i[\'’]?m not) '
    r'(?:have|offer|take|accept|supervise).{0,60}(?:position|opening|student|funding|supervision)\b',
    r'\bunable to (?:offer|take|accept|supervise).{0,60}(?:position|student|you|supervision)\b',
    r'\bapplication (?:was|has been) (?:unsuccessful|rejected|declined)\b',
    r'\bregret to inform you\b',
    r'\bwe have decided to move forward with other candidates\b',
    r'\bnot been selected\b',
    r'\bnot shortlisted\b',
    r'\bnot successful\b',
    r'\bposition has been filled\b',
    r'\bhas been filled\b',
    # 2026-10-15 (Ganian/Mathieu): capacity/funding refusals without the
    # hedge words the _HEDGE_NEAR gate looks for.
    r"\bi(?:'|’)?m sorry,? i can(?:'|’)?t\b",
    r'\b(?:more than )?enough students\b',
    r'\bnot aware of any\b.{0,60}\b(?:funding|opportunit|position|opening)s?\b',
    # 2026-10-19 (Vijay/Santhoshini MMath): capacity/hiring-plan refusals.
    r'\bat capacity\b.{0,60}\b(?:for|of)?\s*(?:students|phd|master)?s?\b',
    r'\bplanning to hire only\b',
    r'\bhiring only (?:phd |master[\'’]?s? )?students?\b',
]
GO_APPLY_PATTERNS = [
    r'\bapply via (?:the )?portal\b',
    r'\bapply through (?:the )?(?:portal|website|system|program)\b',
    r'\bsubmit (?:an |your )?application through\b',
    r'\bapply formally\b',
    r'\bplease apply (?:through|via|to)\b',
    r'\b(?:position|opening) (?:is )?listed on .{0,30}\bwebsite\b',
    r'\bapply online\b',
    r'\bapply on the (?:website|portal)\b',
    r'\bofficial application\b',
    r'\bthrough the official\b',
    r'\bsubmit your application (?:at|via|on)\b',
    r'\bapplication portal\b',
    r'\byou should apply\b',
    r'\bplease submit\b',
]
CONDITIONAL_PATTERNS = [
    r'\bhappy to supervise if\b',
    r'\bcould work if\b',
    r'\bif you can get funding\b',
    r'\bdepend(?:s)? on (?:available )?funding\b',
    r'\bwould need to secure (?:a )?(?:scholarship|funding)\b',
    r'\bsubject to (?:available )?funding\b',
    r'\bif you are admitted\b',
    r'\bconditional on\b',
    r'\bprovided that\b',
    r'\bas long as you\b',
    r'\bpending (?:funding|admission|approval)\b',
    r'\bif (?:funding|a scholarship) (?:is|becomes) available\b',
    r'\bonce you have (?:secured|obtained)\b',
]
ACCEPTED_PATTERNS = [
    r'\bhappy to supervise(?! if)\b',
    r'\bwelcome to join (?:my|our|the) (?:group|lab|team)\b',
    r'\byou (?:have been|are) accepted\b',
    r'\bposition is yours\b',
    r'\b(?:offer|offering) you (?:the |a )?(?:phd |postdoc )?position\b',
    r'\bpleased to offer you\b',
    r'\bcongratulations\b',
    r'\boffer you admission\b',
    r'\byou have been admitted\b',
    r'\bpleased to inform you that you\b',
    r'\bwould be glad to supervise(?! if)\b',
]

HOLDING_PATTERNS = [
    r'we have received your application',
    r'application (?:has been )?received',
    r'application (?:is|remains) under review',
    r'application (?:is )?complete',
    r'application (?:is )?incomplete',
    r'documents? (?:have been )?received',
    # 2026-10-15 (UNIST/POSTECH admin acks):
    r'\byour (?:submitted )?documents? (?:have|were|are) (?:been )?(?:uploaded|received|submitted)\b',
    r'\bdocuments? (?:were|have been) (?:uploaded|received|submitted) successfully\b',
    r'\bwe have received your .{0,40}\b(?:submission|score|documents?)\b',
    r'\bwill come back to\b',
    r'\bwill get back to you\b',
    r'\bwill be in touch\b',
    r'\bshortlisted\b',
    r'\bcurrently (?:reviewing|taking a look)\b',
    r'\bbeing reviewed\b',
    r'\bunder review\b',
    r'\bwe are reviewing\b',
    r'\bwill contact (?:you|the shortlisted)\b',
]

SUBMITTED_PATTERNS = [
    # 2026-10-15: "completed my ONLINE application" / "successfully submitted
    # my online application" broke the strict verb+possessive forms.
    r"\bi\b.{0,30}\b(?:submitted|completed|filed)\b.{0,40}\bapplication\b",
    r"\bi (?:have |['’]ve )?(?:just )?applied\b",
    r'\bsubmitted my application\b',
    r'\bmy application (?:has been|was) submitted\b',
]

_HEDGE_RE = re.compile(r'\b(?:unfortunately|regret)\b', re.I)
_HEDGE_NEAR = [
    r'\bbusy\b',
    r'\bfull\b',
    r'\bno capacity\b',
    r'\bnot able to\b',
    r'\bunable to\b',
    r'\bcannot\b',
    r"\bcan['’]t\b",
    r"\bwon['’]t be able\b",
    r'\bnot currently\b',
    r'\bfully committed\b',
    r'\bdo not (?:\w+ )?think i can\b',
    r'\balready have enough students\b',
    r'\bnot taking\b',
    r'\bnot accepting\b',
    r'\bnot admitting\b',
    r'\bno funding\b',
]
_HEDGE_NEAR_RE = [re.compile(p, re.I) for p in _HEDGE_NEAR]

CLASS_ORDER = ('REJECTED', 'GO_APPLY', 'CONDITIONAL', 'ACCEPTED', 'HOLDING')
# Bump when any CLASS_PATTERNS entry changes: invalidates cached summaries
# so threads are re-classified under the new rules (see _ensure_cache).
CLASSIFIER_VERSION = '2026-10-22.1'
CLASS_PATTERNS = {
    'REJECTED': REJECTED_PATTERNS,
    'GO_APPLY': GO_APPLY_PATTERNS,
    'CONDITIONAL': CONDITIONAL_PATTERNS,
    'ACCEPTED': ACCEPTED_PATTERNS,
    'HOLDING': HOLDING_PATTERNS,
}
CLS_TO_STATUS = {
    'REJECTED': 'rejected',
    'ACCEPTED': 'accepted',
    'CONDITIONAL': 'conditional',
    'GO_APPLY': 'go_apply',
}
DECISIVE_CLS = set(CLS_TO_STATUS)
TERMINAL_STATUS = {'accepted', 'conditional', 'rejected'}
NON_TERMINAL_STATUS = {'ongoing', 'processing', 'follow_up', 'no_reply'}

ACK_STARTS = (
    'thanks', 'thank you', 'ok', 'okay', 'great', 'noted',
    'got it', 'sounds good', 'perfect',
)
NUDGE_KEEP = ('follow', 'remind', 'checking', 'hear from', 'response', 'reply')
AUTO_LOCAL_PREFIXES = (
    'noreply', 'no-reply', 'donotreply', 'do-not-reply', 'application-management', 'hr',
)
OOO_SUBJECT_PREFIXES = (
    'automatic reply', 'out of office', 'autoreply', 'undeliverable',
    'delivery status notification',
)
_EMOJI_RE = re.compile(
    '['
    '\U0001F300-\U0001FAFF'
    '\u2600-\u27BF'
    ']+'
)


def get_gmail_service():
    """Get authorized Gmail API service."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    with open(TOKEN_PATH) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(token_data, f, indent=2)

    # 2026-10-25: longer socket timeout — the 10s httplib2 default plus
    # Iran-net latency killed runs on connect; gmail_execute retries handle
    # the residual failures.
    import httplib2
    import google_auth_httplib2
    return build(
        "gmail", "v1",
        http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30)),
        cache_discovery=False,
    )


def get_or_create_label(service, name):
    """Get label ID by name, create if missing."""
    results = gmail_execute(service.users().labels().list(userId='me'))
    labels = results.get('labels', [])
    for label in labels:
        if label['name'] == name:
            return label['id']

    # Create label only in a real run. A dry-run must not mutate Gmail.
    if DRY_RUN:
        raise RuntimeError(f"Required Gmail label missing during dry-run: {name}")
    body = {'name': name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
    label = gmail_execute(service.users().labels().create(userId='me', body=body))
    return label['id']


def is_genuine_application(subject, body, to_addr):
    """Filter to actual PhD/Masters/Postdoc applications only."""
    text = (subject + ' ' + body).lower()

    # 2026-10-13: application-intent in the SUBJECT ("Prospective MPhil
    # applicant") is decisive on its own; broad 'application' in the body
    # is too weak alone ("Certificate of Deposit" false positive).
    # 2026-10-14: same for degree-name subjects ("Eligibility question,
    # MEng by Research, August 2027 intake") — no keyword matched the body.
    subj_text = (subject or '').lower()
    if re.search(r'\b(prospective|applicant|mphil|m\.sc|msc|m\.?eng)\b', subj_text):
        return not any(re.search(rf'\b{re.escape(pat)}\b', text)
                       for pat in EXCLUDE_PATTERNS)

    # Must contain academic keywords
    if not any(kw in text for kw in ACADEMIC_KEYWORDS):
        return False

    # Must NOT match exclusion patterns (word-boundary: 'rent' must not hit
    # 'different'/'current' — 2026-09-15 Gaubert M2 application killed)
    if any(re.search(rf'\b{re.escape(pat)}\b', text) for pat in EXCLUDE_PATTERNS):
        return False

    # Must be addressed to a professor/institution (not self, not support)
    if not to_addr or to_addr == 'me':
        return False

    # Skip self-sent emails
    my_emails = ['youruser']  # tokens identifying your own addresses
    if any(email in to_addr.lower() for email in my_emails):
        return False

    return True


def extract_body(parts):
    """Recursively collect text/plain parts from a Gmail payload."""
    text = ""
    for part in parts:
        mime = part.get('mimeType', '')
        if mime == 'text/plain':
            data = part.get('body', {}).get('data', '')
            if data:
                text += base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
        elif 'parts' in part:
            text += extract_body(part['parts'])
    return text


def _header(msg_data, name):
    headers = {h['name'].lower(): h['value'] for h in msg_data.get('payload', {}).get('headers', [])}
    return headers.get(name.lower(), '')


def is_re_subject(subject):
    return bool(re.match(r'^\s*(re|fw|fwd)\s*:', subject or '', re.I))


_thread_label_cache = {}


def thread_has_label(service, thread_id, label_id):
    """True if ANY message in the thread already carries label_id.

    Result cached per thread (2026-10-10): follow-ups in the same thread
    re-fetched it once per message and burned Gmail quota.
    """
    if not thread_id:
        return False
    if thread_id in _thread_label_cache:
        return label_id in _thread_label_cache[thread_id]
    try:
        t = gmail_execute(service.users().threads().get(
            userId='me', id=thread_id, format='minimal',
        ))
    except Exception:
        return False
    labels = set()
    for tm in t.get('messages', []):
        labels.update(tm.get('labelIds', []))
    _thread_label_cache[thread_id] = labels
    return label_id in labels


def _decode_b64(data):
    if not data:
        return ''
    return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')


def _strip_html(s):
    s = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', s)
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'(?i)</p>', '\n', s)
    s = re.sub(r'<[^>]+>', ' ', s)
    return htmlmod.unescape(s)


def _collect_body(parts, plain, html):
    for part in parts:
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data', '')
        if mime == 'text/plain' and data:
            plain.append(_decode_b64(data))
        elif mime == 'text/html' and data:
            html.append(_decode_b64(data))
        elif 'parts' in part:
            _collect_body(part['parts'], plain, html)


def _message_body_text(msg):
    payload = msg.get('payload', {}) or {}
    plain, html = [], []
    if 'parts' in payload:
        _collect_body(payload['parts'], plain, html)
    else:
        data = payload.get('body', {}).get('data', '')
        mime = payload.get('mimeType', '')
        if mime == 'text/html':
            html.append(_decode_b64(data))
        elif data:
            plain.append(_decode_b64(data))
    if plain:
        return ''.join(plain)
    if html:
        return _strip_html(''.join(html))
    return ''


def _aware(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=UTC)
    s = str(value).replace('Z', '+00:00')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _email_addr(value):
    _, addr = parseaddr(value or '')
    return (addr or '').lower()


def _local_part(value):
    addr = _email_addr(value)
    return addr.split('@', 1)[0] if addr else ''


def _headers_of(m):
    h = m.get('headers')
    if isinstance(h, dict):
        return {str(k).lower(): v for k, v in h.items()}
    return {}


def clean_body(text):
    """Drop quoted history and cheap signature lines before matching."""
    if not text:
        return ''
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    cut = None
    for i in range(len(lines)):
        if re.match(r'(?i)^\s*-{3,}Original Message-{3,}\s*$', lines[i]):
            cut = i
            break
        for extra in (0, 1, 2):
            chunk = ' '.join(x.strip() for x in lines[i:i + 1 + extra])
            if re.search(r'(?i)^On .+ wrote:\s*$', chunk):
                cut = i
                break
        else:
            continue
        break
    if cut is not None:
        lines = lines[:cut]
    seen = False
    cut = None
    for i, line in enumerate(lines):
        if line.strip():
            if seen and re.match(r'(?i)^\s*(From|Da|Von|De)\s*:\s+\S', line):
                cut = i
                break
            seen = True
    if cut is not None:
        lines = lines[:cut]
    kept = []
    for line in lines:
        if line.lstrip().startswith('>'):
            continue
        if re.match(r'(?i)^sent from my\b', line.strip()):
            continue
        kept.append(line)
    return '\n'.join(kept).strip()


def _emoji_only(text):
    t = _EMOJI_RE.sub('', text)
    t = re.sub(r'[\W_]+', '', t, flags=re.UNICODE)
    return not t.strip()


def _starts_ack(low):
    s = low.strip()
    for a in ACK_STARTS:
        if s == a or s.startswith(a + ' ') or s.startswith(a + '!') or s.startswith(a + ',') or s.startswith(a + '.'):
            return True
    return False


def _is_me_noise(cleaned_low):
    if 'reacted via gmail' in cleaned_low:
        return True
    words = re.findall(r'\S+', cleaned_low)
    if len(words) > 12:
        return False
    if not (_starts_ack(cleaned_low) or _emoji_only(cleaned_low)):
        return False
    return not any(term in cleaned_low for term in NUDGE_KEEP)


def _is_auto_reply(headers, from_header, subject):
    auto_sub = (headers.get('auto-submitted') or '').strip().lower()
    if auto_sub and auto_sub != 'no':
        return True
    if headers.get('x-autoreply') or headers.get('x-autorespond'):
        return True
    prec = (headers.get('precedence') or '').strip().lower()
    if prec in ('auto_reply', 'bulk', 'junk'):
        return True
    local = _local_part(from_header)
    if local in ('mailer-daemon', 'postmaster'):
        return True
    subj = (subject or '').strip().lower()
    return any(subj.startswith(p) for p in OOO_SUBJECT_PREFIXES)


def _is_system_sender(from_header):
    local = _local_part(from_header)
    return any(local.startswith(p) for p in AUTO_LOCAL_PREFIXES)


def _hedge_rejected(text):
    for m in _HEDGE_RE.finditer(text):
        lo = max(0, m.start() - 250)
        hi = min(len(text), m.end() + 250)
        window = text[lo:hi]
        if any(p.search(window) for p in _HEDGE_NEAR_RE):
            return True
    return False


def _match_classes(text):
    matched = []
    for cls in CLASS_ORDER:
        hit = any(re.search(p, text, re.I) for p in CLASS_PATTERNS[cls])
        if cls == 'REJECTED' and not hit:
            hit = _hedge_rejected(text)
        if hit:
            matched.append(cls)
    if not matched:
        return 'SUBSTANTIVE', []
    winner = matched[0]
    # Portal instruction is GO_APPLY unless a rejection or condition also matched.
    if winner == 'GO_APPLY' and 'CONDITIONAL' in matched and 'REJECTED' not in matched:
        winner = 'CONDITIONAL'
    return winner, matched


def _is_submitted(text):
    return any(re.search(p, text, re.I) for p in SUBMITTED_PATTERNS)


def _role_of(m):
    if m.get('role') in ('me', 'them'):
        return m['role']
    labels = m.get('labels') or []
    if 'SENT' in labels:
        return 'me'
    addr = _email_addr(m.get('from') or _headers_of(m).get('from', ''))
    if addr in MY_ADDRESSES:
        return 'me'
    return 'them'


def _ts_of(m):
    if m.get('ts') is not None:
        return _aware(m['ts'])
    if m.get('internalDate') is not None:
        return _aware(m['internalDate'])
    return datetime.fromtimestamp(0, tz=UTC)


def _normalize_one(m, cycle_start):
    labels = m.get('labels') or []
    if 'DRAFT' in labels:
        return None
    ts = _ts_of(m)
    if ts < cycle_start:
        return None
    headers = _headers_of(m)
    from_h = m.get('from') or headers.get('from', '')
    subject = m.get('subject') or headers.get('subject', '')
    role = _role_of(m)
    cleaned = clean_body(m.get('text') or '')
    low = cleaned.lower()
    if role == 'me':
        if _is_me_noise(low):
            return None
        return {
            'role': 'me',
            'ts': ts,
            'cls': None,
            'submitted': _is_submitted(low),
            'matched': [],
            'preview': cleaned[:160],
        }
    if _is_auto_reply(headers, from_h, subject):
        return None
    cls, matched = _match_classes(low)
    if _is_system_sender(from_h) and cls not in DECISIVE_CLS and cls != 'HOLDING':
        return None
    return {
        'role': 'them',
        'ts': ts,
        'cls': cls,
        'submitted': False,
        'matched': matched,
        'preview': cleaned[:160],
    }


def _prepare_messages(messages, cycle_start=None):
    start = cycle_start if cycle_start is not None else CYCLE_START
    out = []
    for m in messages:
        if 'text' not in m:
            if m.get('cls') == 'IGNORE':
                continue
            item = {
                'role': m['role'],
                'ts': _aware(m['ts']),
                'cls': m.get('cls'),
                'submitted': bool(m.get('submitted')),
                'matched': list(m.get('matched') or []),
                'preview': m.get('preview') or '',
            }
            if item['ts'] < start:
                continue
            out.append(item)
            continue
        item = _normalize_one(m, start)
        if item is not None:
            out.append(item)
    out.sort(key=lambda x: x['ts'])
    return out


@dataclass(frozen=True)
class Decision:
    status: str | None
    reason: str
    needs_review: bool = False
    ambiguous: bool = False


def _needs_review_flags(M, now):
    ambiguous = any(len(m.get('matched') or []) >= 2 for m in M if m['role'] == 'them')
    their = [m for m in M if m['role'] == 'them']
    recent_sub = False
    if their:
        last = their[-1]
        age_h = (now - last['ts']).total_seconds() / 3600.0
        if last.get('cls') == 'SUBSTANTIVE' and age_h <= 7 * 24:
            recent_sub = True
    return ambiguous or recent_sub, ambiguous


def classify_thread(messages, now, existing_status_keys):
    """Pure status decision. No Gmail or DB access."""
    now = _aware(now)
    existing = list(existing_status_keys or [])
    M = _prepare_messages(messages)
    review, ambiguous = _needs_review_flags(M, now)

    def finish(status, reason):
        if (
            status in NON_TERMINAL_STATUS
            and any(k in TERMINAL_STATUS for k in existing)
        ):
            return Decision(None, f'guard: keep manual terminal ({existing}) vs {status}', review, ambiguous)
        return Decision(status, reason, review, ambiguous)

    if not M:
        return Decision(None, 'empty: no in-cycle messages', False, False)

    their_msgs = [m for m in M if m['role'] == 'them']
    last_decisive = None
    for m in their_msgs:
        if m.get('cls') in DECISIVE_CLS:
            last_decisive = m
    holding_seen = any(m.get('cls') == 'HOLDING' for m in their_msgs) or any(
        m['role'] == 'me' and m.get('submitted') for m in M
    )

    # Terminal replies stick; go_apply yields if they later hold or I submitted.
    if last_decisive is not None:
        superseded = False
        if last_decisive['cls'] == 'GO_APPLY':
            later_hold = any(
                m['ts'] > last_decisive['ts'] and m.get('cls') == 'HOLDING'
                for m in their_msgs
            )
            later_sub = any(
                m['role'] == 'me' and m.get('submitted') and m['ts'] > last_decisive['ts']
                for m in M
            )
            if later_hold or later_sub:
                superseded = True
                holding_seen = True
        if not superseded:
            return finish(
                CLS_TO_STATUS[last_decisive['cls']],
                f'terminal: {last_decisive["cls"]} at {last_decisive["ts"].isoformat()}',
            )

    last = M[-1]
    if last['role'] == 'them':
        if last.get('cls') == 'HOLDING':
            return finish('processing', 'them-last: HOLDING')
        return finish('ongoing', f'them-last: {last.get("cls")}')

    last_their = their_msgs[-1] if their_msgs else None
    if last_their is None:
        R = [m for m in M if m['role'] == 'me']
    else:
        R = [m for m in M if m['role'] == 'me' and m['ts'] > last_their['ts']]
    n = len(R)
    if last_their is None:
        nudges = n - 1
        last_their_cls = 'none'
    elif last_their.get('cls') == 'HOLDING':
        nudges = n
        last_their_cls = 'HOLDING'
    else:
        nudges = n - 1
        last_their_cls = str(last_their.get('cls') or 'none')
    if n == 0:
        return finish('ongoing', 'waiting: empty R')
    hours = (now - R[-1]['ts']).total_seconds() / 3600.0
    days = hours / 24.0
    reason = f'waiting: nudges={nudges} days={days:.1f} last_their={last_their_cls}'
    if hours < WAIT_HOURS:
        if nudges == 0 and holding_seen:
            return finish('processing', reason)
        return finish('ongoing', reason)
    if nudges >= NO_REPLY_NUDGES:
        return finish('no_reply', reason)
    return finish('follow_up', reason)


# Time-driven ladder: a thread that goes quiet escalates on its own,
# without any new mail. Only this one step is allowed without new mail.
#   ongoing -> follow_up   (5+ days silent)
# Everything else needs new mail, so a hand-set label is never clobbered
# by the passage of time.
TIME_LADDER_STEP = ('ongoing', 'follow_up')
# Weak verdicts carry no decision news (no accept/reject/apply/hold);
# everything else is decisive. A weak verdict may never DOWNGRADE a
# decisive label, whoever set it.
WEAK_STATUS = ('ongoing', 'follow_up', 'no_reply')


def allow_status_write(target, existing, cron_wrote, new_mail):
    """May the cron write `target` over the thread's real labels?

    existing = the status labels actually on the thread right now.
    cron_wrote = what THIS script last wrote (cache cron_status); None
      means the cron never wrote here, so a labeled thread is hand-set
      of unknown provenance — treat as a manual override.
    new_mail = a message landed since the last pass (message-id set changed).

    - nothing labeled yet: first classification, write freely.
    - NOTHING labeled but the cron wrote one before: the user REMOVED it
      (the cron never strips a thread to no status label). Removal means
      the user closed the thread. Silence-derived weak verdicts must
      never re-add a label; only decisive news carried by new mail may
      resurrect the thread. 2026-10-21: user kept removing follow_up
      ("those have ended") and the cron re-added it every hour because
      the empty-label case fell into the first-classification branch.
    - weak verdict (ongoing/follow_up/no_reply):
      * never downgrades a decisive label (go_apply/processing/terminal),
        cron-set or hand-set. 2026-10-20 incident: a professor's
        conversational reply ("here are the scholarship options") made
        the cron compute weak 'ongoing' and it erased a standing
        'go_apply' — conversational churn must never do that.
      * on a cron-owned weak label: the quiet-thread escalation
        (ongoing -> follow_up) works on time alone; other weak moves
        need new mail.
      * on a hand-set label: held, always.
    - decisive verdict (terminal/go_apply/processing): new mail carries
      it (this is what voids a manual override); without new mail it is
      a classifier re-judge, which may land only on cron-owned labels.
    """
    if not target or target in (existing or []):
        return False
    if not (existing or []):
        # User REMOVED the label the cron once wrote: closed by hand.
        # Weak verdicts (all silence-derived) never re-add. Decisive
        # news may, but only with new mail.
        if cron_wrote:
            return target not in WEAK_STATUS and bool(new_mail)
        return True  # never status-labeled: first classification writes
    cron_owned = list(existing) == [cron_wrote]
    cur = existing[0]
    if target in WEAK_STATUS:
        if cur not in WEAK_STATUS:
            return False  # weak verdict never downgrades a decisive label
        if not cron_owned:
            return False  # hand-set weak label: held until decisive news
        return (cur, target) == TIME_LADDER_STEP or bool(new_mail)
    return bool(new_mail) or cron_owned


def gmail_thread_to_messages(thread):
    out = []
    for msg in thread.get('messages') or []:
        headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
        out.append({
            'ts': int(msg.get('internalDate') or 0),
            'text': _message_body_text(msg),
            'labels': list(msg.get('labelIds') or []),
            'headers': headers,
            'from': headers.get('from', ''),
            'subject': headers.get('subject', ''),
        })
    out.sort(key=lambda m: m['ts'])
    return out


def _thread_subject(thread, raw_messages):
    for m in reversed(raw_messages):
        if m.get('subject'):
            return m['subject']
    msgs = thread.get('messages') or []
    if msgs:
        headers = {h['name'].lower(): h['value'] for h in msgs[0].get('payload', {}).get('headers', [])}
        return headers.get('subject', '')
    return ''


def summary_payload(subject, prepared):
    messages = []
    for m in prepared:
        item = {
            'role': m['role'],
            'ts': m['ts'].astimezone(UTC).isoformat(),
            'submitted': bool(m.get('submitted')),
            'matched': list(m.get('matched') or []),
            'preview': m.get('preview') or '',
        }
        if m.get('cls'):
            item['cls'] = m['cls']
        messages.append(item)
    return {'subject': subject, 'messages': messages}


_last_req_at = 0.0


def gmail_execute(req, num_retries=5):
    """Throttle ~5 req/s; back off on 403/429 quota bursts AND retry
    network-level failures (connect timeouts, reset peers). Iran-net TCP
    connects to googleapis.com die often enough that a bare req.execute()
    killed whole cron runs at 2026-10-25 05:00 and 11:00."""
    global _last_req_at
    from googleapiclient.errors import HttpError
    backoff = 1.0
    last_err = None
    for attempt in range(6):
        wait = _MIN_REQ_INTERVAL - (time.monotonic() - _last_req_at)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_req_at = time.monotonic()
            return req.execute(num_retries=num_retries)
        except HttpError as e:
            last_err = e
            status = getattr(getattr(e, 'resp', None), 'status', None)
            msg = str(e)
            if status in (403, 429) or 'rateLimitExceeded' in msg or 'Total Query Cost' in msg:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise
        except (TimeoutError, ConnectionError, OSError) as e:
            # socket.timeout == TimeoutError (3.10+); ssl/reset errors are
            # ConnectionError/O subclasses. http_client errors surface as
            # OSError too. Retry with the same backoff ladder.
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
    if last_err:
        raise last_err
    raise RuntimeError('gmail_execute failed')


def _list_threads(service, query):
    found = {}
    token = None
    while True:
        kwargs = {'userId': 'me', 'q': query, 'maxResults': 500}
        if token:
            kwargs['pageToken'] = token
        resp = gmail_execute(service.users().threads().list(**kwargs))
        for t in resp.get('threads') or []:
            found[t['id']] = str(t.get('historyId') or '')
        token = resp.get('nextPageToken')
        if not token:
            break
    return found


def list_application_threads(service):
    """Parent label search misses nested children; union parent + each status."""
    found = {}
    queries = [f'label:"{LABEL_BASE}"']
    for display in LABELS_STATUS.values():
        queries.append(f'label:"{display}"')
    seen_q = []
    for q in queries:
        if q in seen_q:
            continue
        seen_q.append(q)
        found.update(_list_threads(service, q))
    return found


def _ensure_cache(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gmail_thread_cache (
            thread_id TEXT PRIMARY KEY,
            history_id TEXT,
            summary_json TEXT,
            status TEXT,
            msg_ids TEXT,
            cron_status TEXT,
            updated_at TEXT
        )"""
    )
    for col in ('msg_ids', 'cron_status'):
        try:
            conn.execute(f"ALTER TABLE gmail_thread_cache ADD COLUMN {col} TEXT")
        except _sq.OperationalError:
            pass  # column already exists
    # 2026-10-17 bootstrap: pre-migration rows have no cron_status — adopt
    # the last observed status as cron-owned so a manual edit made after the
    # last cron run is detected as an override on the very next pass.
    # 2026-10-20 fix: only adopt when a msg_ids snapshot exists (written by
    # the post-schema code that actually performs the Gmail write). Rows
    # without a snapshot predate the verified-write path — their status may
    # never have landed on Gmail (11 follow_up threads were exactly this),
    # so claiming ownership froze them: cache-hit runs saw target==existing
    # and skipped the write, refetch runs saw labels!=cron_wrote and held a
    # phantom "override". No snapshot -> cron_status stays NULL -> the next
    # refetch treats the thread as first-classification and writes.
    conn.execute(
        "UPDATE gmail_thread_cache SET cron_status = status"
        " WHERE cron_status IS NULL AND status IS NOT NULL"
        " AND msg_ids IS NOT NULL"
    )
    # 2026-10-20 one-time repair: rows adopted by the buggy pre-fix
    # bootstrap (cron_status set despite no msg_ids snapshot). Clear the
    # false claim and force a refetch so the status actually lands.
    # (gmail_cache_meta is created two blocks below on first run; guard.)
    try:
        row = conn.execute(
            "SELECT value FROM gmail_cache_meta WHERE key='preschema_repair_done'"
        ).fetchone()
    except _sq.OperationalError:
        row = None  # table not created yet -> first run -> repair proceeds
    if not row:
        conn.execute(
            "UPDATE gmail_thread_cache SET cron_status = NULL, summary_json = NULL"
            " WHERE msg_ids IS NULL"
        )
        conn.execute(
            "INSERT OR REPLACE INTO gmail_cache_meta(key, value) VALUES('preschema_repair_done', '1')"
        )
    # 2026-10-15: cached message classes are produced by the pattern sets.
    # Bump CLASSIFIER_VERSION whenever a pattern changes so stale cached
    # summaries (classified under old rules) are re-fetched and re-judged
    # instead of replaying old verdicts forever.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gmail_cache_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    row = conn.execute(
        "SELECT value FROM gmail_cache_meta WHERE key='classifier_version'"
    ).fetchone()
    if not row or row[0] != CLASSIFIER_VERSION:
        # 2026-10-17: null only summaries — threads are re-fetched and
        # re-judged under the new rules, but status + msg_ids MUST
        # survive: they are the override memory (what the cron last
        # wrote, the message set at last pass). Deleting whole rows
        # here would erase held manual overrides for one pass and the
        # cron would revert every user-edited label.
        conn.execute("UPDATE gmail_thread_cache SET summary_json = NULL")
        conn.execute(
            "INSERT OR REPLACE INTO gmail_cache_meta(key, value) VALUES('classifier_version', ?)",
            (CLASSIFIER_VERSION,),
        )
    conn.commit()


def _load_cycle_start():
    try:
        conn = _sq.connect(str(DB_PATH), timeout=5)
        row = conn.execute(
            "SELECT value FROM config WHERE key='application_cycle_start'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            return _aware(row[0])
    except Exception:
        pass
    return CYCLE_START


def run():
    global CYCLE_START
    print(f"Starting Gmail Label Applications run at {datetime.now()}")
    if DRY_RUN:
        print("DRY-RUN enabled: Gmail labels will not be modified")

    CYCLE_START = _load_cycle_start()
    service = get_gmail_service()

    # Look back to last successful scan, or 24h if no prior scan
    try:
        _conn = _sq.connect(str(DB_PATH))
        _row = _conn.execute("SELECT value FROM config WHERE key='gmail_last_scan_at'").fetchone()
        _conn.close()
        if _row and _row[0]:
            from dateutil.parser import parse as _parse
            last_scan = _parse(_row[0])
            hours_back = (datetime.now() - last_scan).total_seconds() / 3600
            # 2026-10-10: window = time since last successful scan (+2d margin),
            # capped at 31d. The old hard 30d floor re-fetched a month of sent
            # mail every run and blew the Gmail per-user quota burst limit
            # (403 'Total Query Cost'); the 2026-09-28 window miss is covered
            # because the window grows when runs fail or stop.
            lookback_days = min(31, max(2, int(hours_back / 24) + 2))
            print(f"Last scan was {hours_back:.1f}h ago — looking back {lookback_days} days")
        else:
            lookback_days = 1
            print("No prior scan found — looking back 1 day")
    except Exception:
        lookback_days = 1
        print("Could not read last scan time — looking back 1 day")

    yesterday = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y/%m/%d')

    # Ensure labels exist
    label_base_id = get_or_create_label(service, LABEL_BASE)
    label_ids = {}
    for name, display in LABELS_STATUS.items():
        label_ids[name] = get_or_create_label(service, display)

    # --- Step 1: Label sent application emails ---
    print(f"Step 1: Scanning sent emails after {yesterday}")
    query = f'in:sent after:{yesterday}'
    results = gmail_execute(service.users().messages().list(userId='me', q=query, maxResults=500))
    messages = results.get('messages', [])

    sent_labeled_count = 0
    newly_labeled_threads = set()

    # 2026-10-13 (Tim Ophelders): decision emails for portal applications
    # arrive as RECEIVED mail in threads with no sent mail, so Step 1 can
    # never reach them and Step 2 only scans base-labeled threads. Sweep
    # inbox decision-like mail (human or system sender) and base-label
    # those threads. Decision classes only (rejection/go-apply/accepted/
    # conditional) — HOLDING would auto-label every random notification.
    print("Step 1b: Scanning received decision emails after " + yesterday)
    dec_query = f'in:inbox after:{yesterday} (decision OR "regret to inform" OR "not selected" OR "application status" OR "we are unable")'
    dec_results = gmail_execute(service.users().messages().list(userId='me', q=dec_query, maxResults=200))
    for msg in dec_results.get('messages', []):
        md = gmail_execute(service.users().messages().get(
            userId='me', id=msg['id'], format='metadata',
            metadataHeaders=['From', 'Subject'],
        ))
        subject = _header(md, 'subject')
        frm = _header(md, 'from')
        text = (subject + ' ' + (md.get('snippet') or '')).lower()
        cls, _matched = _match_classes(text)
        if cls not in DECISIVE_CLS:
            continue
        thread_id = md.get('threadId')
        if thread_id and not thread_has_label(service, thread_id, label_base_id):
            if not DRY_RUN:
                gmail_execute(service.users().messages().modify(
                    userId='me', id=msg['id'],
                    body={'addLabelIds': [label_base_id]}
                ))
            verb = "Would label" if DRY_RUN else "Labeled"
            print(f"  {verb} decision (inbox): {subject[:50]}")
            sent_labeled_count += 1
            newly_labeled_threads.add(thread_id)

    for msg in messages:
        msg_data = gmail_execute(service.users().messages().get(
            userId='me', id=msg['id'], format='full',
        ))

        headers = {h['name'].lower(): h['value'] for h in msg_data.get('payload', {}).get('headers', [])}
        subject = headers.get('subject', '')
        to_addr = headers.get('to', '')

        # Full body for filtering — snippet is only ~200 chars and misses
        # keywords that appear later (2026-09-15: Gaubert M2 application lost)
        if 'parts' in msg_data.get('payload', {}):
            snippet = extract_body(msg_data['payload']['parts'])
        else:
            snippet = base64.urlsafe_b64decode(
                msg_data.get('payload', {}).get('body', {}).get('data', '') or ''
            ).decode('utf-8', errors='replace')

        # Re: in a thread that already carries the base label = known
        # application thread. Label follow-ups even when the keyword gate
        # fails (2026-09-28: Gaubert M2 / Bei Xiaohui follow-ups missed —
        # academic keywords absent, exclude 'room' hit a genuine PhD thread).
        # Exclusions still apply so deferral/housing sub-threads stay out.
        if is_re_subject(subject) and thread_has_label(
            service, msg_data.get('threadId'), label_base_id
        ):
            text = (subject + ' ' + snippet).lower()
            if not any(re.search(rf'\b{re.escape(pat)}\b', text)
                       for pat in EXCLUDE_PATTERNS):
                if label_base_id not in msg_data.get('labelIds', []):
                    if not DRY_RUN:
                        gmail_execute(service.users().messages().modify(
                            userId='me', id=msg['id'],
                            body={'addLabelIds': [label_base_id]}
                        ))
                    verb = "Would label" if DRY_RUN else "Labeled"
                    print(f"  {verb} sent (thread): {subject[:50]}")
                    sent_labeled_count += 1
                    newly_labeled_threads.add(msg_data.get('threadId'))
                continue

        if not is_genuine_application(subject, snippet, to_addr):
            continue

        current_labels = msg_data.get('labelIds', [])
        if label_base_id in current_labels:
            continue

        # Re:/Fwd: gate (2026-08-28): a reply only gets the application
        # label if its thread already carries it (a known application
        # thread) or the text shows strong application intent. This stops
        # research-collaboration / meeting-scheduling replies ("Dear
        # Professor X, Wednesday works for me...") from being labeled.
        if is_re_subject(subject):
            already = thread_has_label(
                service, msg_data.get('threadId'), label_base_id
            )
            strong = any(
                kw in (subject + ' ' + snippet).lower()
                for kw in STRONG_APPLICATION_KW
            )
            # 2026-10-16: degree token in the SUBJECT ("Re: Meeting for
            # MSc VinUni") carries the intent when the sender's client
            # split the follow-up into a fresh thread.
            subj_degree = bool(re.search(
                r'\b(phd|mphil|m\.sc|msc|m\.?eng|master[\'’]?s?)\b',
                (subject or '').lower(),
            ))
            if not already and not strong and not subj_degree:
                print(f"  Skipped reply in unlabeled thread: {subject[:50]}")
                continue

        if not DRY_RUN:
            gmail_execute(service.users().messages().modify(
                userId='me', id=msg['id'],
                body={'addLabelIds': [label_base_id]}
            ))
        verb = "Would label" if DRY_RUN else "Labeled"
        print(f"  {verb} sent: {subject[:50]}")
        sent_labeled_count += 1
        newly_labeled_threads.add(msg_data.get('threadId'))

    # --- Step 2: Classify every thread under the base label ---
    print(f"Step 2: Scanning all threads under {LABEL_BASE!r} (no after: filter)")
    now = datetime.now(UTC)
    status_ids = list(dict.fromkeys(label_ids.values()))
    id_to_key = {lid: name for name, lid in label_ids.items()}

    def resolve_status_id(status_key):
        return label_ids.get(status_key)

    def current_status_keys(thread_msgs):
        found = []
        for tm in thread_msgs:
            for lid in tm.get('labelIds', []):
                name = id_to_key.get(lid)
                if name and name not in found:
                    found.append(name)
        return found

    conn = _sq.connect(str(DB_PATH), timeout=30)
    cache = {}
    if not DRY_RUN:
        _ensure_cache(conn)
    try:
        rows = conn.execute(
            "SELECT thread_id, history_id, summary_json, status, msg_ids, cron_status"
            " FROM gmail_thread_cache"
        ).fetchall()
        cache = {
            r[0]: {
                'history_id': r[1], 'summary_json': r[2], 'status': r[3],
                'msg_ids': r[4], 'cron_status': r[5],
            } for r in rows
        }
    except _sq.OperationalError:
        cache = {}

    threads = list_application_threads(service)
    status_counts = {}
    status_changes = []
    manual_overrides = []
    needs_review_rows = []
    skipped_empty = []
    replies_classified = 0
    all_rows = []

    for thread_id, history_id in threads.items():
        cached = cache.get(thread_id)
        subject = ''
        existing = []
        prepared = None
        used_cache = False
        if cached and cached.get('history_id') == history_id and cached.get('summary_json'):
            try:
                payload = json.loads(cached['summary_json'])
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                used_cache = True
                if isinstance(payload, dict):
                    subject = payload.get('subject') or ''
                    raw_summary = payload.get('messages') or []
                else:
                    raw_summary = payload
                prepared = _prepare_messages(raw_summary)
                # status = last OBSERVED label set (real labels snapshot,
                # kept fresh even when the summary is cache-reused). On a
                # cache-hit run this is the only real-label record.
                if cached.get('status'):
                    existing = [cached['status']]

        # 2026-10-17: a status write needs NEW MAIL, not merely a changed
        # history_id — Gmail bumps history_id for label edits too, so a
        # manual X->Y relabel looked like a thread change and got reverted
        # within the hour. The thread's message-id set is the real signal.
        # 2026-10-20: the message-id set is needed on EVERY pass (it decides
        # "new mail"), so a cache-hit run fetches it too (labels + ids only,
        # no bodies). Without it, new_mail was always False and no status
        # could ever change.
        prev_msg_ids = None
        msg_ids_now = None
        if not used_cache:
            thread = gmail_execute(
                service.users().threads().get(userId='me', id=thread_id, format='full')
            )
            raw = gmail_thread_to_messages(thread)
            subject = _thread_subject(thread, raw)
            msgs_all = thread.get('messages') or []
            existing = current_status_keys(msgs_all)
            prepared = _prepare_messages(raw)
            # Drafts excluded: composing a reply must not count as new mail.
            msg_ids_now = sorted(
                m['id'] for m in msgs_all if 'DRAFT' not in (m.get('labelIds') or [])
            )
            if cached is not None and cached.get('msg_ids'):
                try:
                    prev_msg_ids = json.loads(cached['msg_ids'])
                except json.JSONDecodeError:
                    prev_msg_ids = None
        else:
            # Cache hit: cheap fetch for real labels + current message ids.
            try:
                fresh = gmail_execute(
                    service.users().threads().get(
                        userId='me', id=thread_id, format='minimal'
                    )
                )
                msgs_all = fresh.get('messages') or []
                existing = current_status_keys(msgs_all)
                msg_ids_now = sorted(
                    m['id'] for m in msgs_all if 'DRAFT' not in (m.get('labelIds') or [])
                )
            except Exception:
                msg_ids_now = None
            if cached is not None and cached.get('msg_ids'):
                try:
                    prev_msg_ids = json.loads(cached['msg_ids'])
                except json.JSONDecodeError:
                    prev_msg_ids = None

        summary_msgs = [{
            'role': m['role'],
            'ts': m['ts'],
            'cls': m.get('cls'),
            'submitted': m.get('submitted'),
            'matched': m.get('matched'),
            'preview': m.get('preview'),
        } for m in (prepared or [])]
        decision = classify_thread(summary_msgs, now, existing)

        current = existing[0] if existing else '-'
        computed = decision.status if decision.status is not None else 'no change'
        final = decision.status if decision.status is not None else (existing[0] if existing else None)

        if decision.reason.startswith('empty:'):
            skipped_empty.append(f"{subject[:40] or thread_id}")

        if decision.needs_review:
            preview = ''
            for m in reversed(prepared or []):
                if m['role'] == 'them':
                    preview = m.get('preview') or ''
                    break
            needs_review_rows.append((subject[:80], preview, decision.reason, computed))

        all_rows.append((current, computed, subject, decision.reason, thread_id, final))
        status_counts[final or 'no change'] = status_counts.get(final or 'no change', 0) + 1

        target = decision.status
        cron_wrote = cached.get('cron_status') if cached else None
        # New mail = the thread's message-id set changed since the last pass.
        new_mail = bool(
            prev_msg_ids and msg_ids_now
            and sorted(prev_msg_ids) != sorted(msg_ids_now)
        )
        will_write = target and target not in existing and allow_status_write(
            target, existing, cron_wrote, new_mail
        )
        add_id = resolve_status_id(target) if (will_write and target) else None
        if will_write:
            if add_id:
                remove = [lid for lid in status_ids if lid and lid != add_id]
                if not DRY_RUN:
                    gmail_execute(service.users().threads().modify(
                        userId='me', id=thread_id,
                        body={'addLabelIds': [add_id], 'removeLabelIds': remove},
                    ))
                old = existing[0] if existing else '-'
                status_changes.append(f"{old} -> {target} | {subject[:40]} | {decision.reason}")
                replies_classified += 1
                final = target
        elif target and target not in existing:
            # Held: a hand-set label stands until new mail arrives. Reported
            # so a manual decision is never silently overwritten.
            manual_overrides.append(
                f"kept {existing[0] if existing else '-'} (cron would say {target}) | {subject[:40]}"
            )
            final = existing[0] if existing else None

        if not DRY_RUN:
            payload = json.dumps(summary_payload(subject, prepared or []), ensure_ascii=False)
            msg_ids_json = (
                json.dumps(msg_ids_now, ensure_ascii=False)
                if msg_ids_now is not None
                else (cached.get('msg_ids') if cached else None)
            )
            # cron_status = what the CRON last wrote (None = never wrote).
            # Only a real write updates it; a held thread keeps its old
            # value so the hold stays visible on the next pass.
            store_cron = target if (will_write and add_id) else (
                cached.get('cron_status') if cached else None
            )
            conn.execute(
                """INSERT OR REPLACE INTO gmail_thread_cache
                   (thread_id, history_id, summary_json, status, msg_ids, cron_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    thread_id, history_id, payload, final, msg_ids_json,
                    store_cron, now.isoformat(),
                ),
            )
            # 2026-10-24: commit per thread. One long Step-2 transaction held
            # the DB write lock across all Gmail fetches (~60-90s) and killed
            # gmail_status_sync's BEGIN IMMEDIATE (15s timeout) whenever both
            # crons fired at :00. Rows are independent and idempotent, so
            # per-thread commit is safe and crash-progress survives.
            conn.commit()

    if not DRY_RUN:
        conn.commit()
    conn.close()

    print("\n--- Summary ---")
    print(f"Sent emails labeled: {sent_labeled_count}")
    print(f"Replies classified: {replies_classified}")
    print("Threads by status:")
    for k in sorted(status_counts, key=lambda x: (-status_counts[x], x or '')):
        print(f"  {k}: {status_counts[k]}")
    if DRY_RUN:
        print("\nCurrent -> computed | subject | reason")
        for current, computed, subject, reason, thread_id, _final in all_rows:
            print(f"  {current} -> {computed} | {(subject or thread_id)[:40]} | {reason}")
    if status_changes:
        print("Status changes:")
        for change in status_changes:
            print(f"  {change}")
    if needs_review_rows:
        print("\nneeds_review:")
        for subject, preview, reason, computed in needs_review_rows:
            print(f"  [{computed}] {subject}")
            print(f"    {preview}")
            print(f"    {reason}")
    if manual_overrides:
        print("\nManual overrides kept (no new mail):")
        for row in manual_overrides:
            print(f"  {row}")
    if skipped_empty:
        print("\nSkipped (empty in-cycle):")
        for row in skipped_empty:
            print(f"  {row}")

    # Stamp gmail_last_scan_at only on a completed run (2026-10-10). It was
    # previously written only by the retired scan_gmail_replies cron, so the
    # labeler window never advanced and every run re-fetched a full month
    # (quota 403 'Total Query Cost'). On failure the stamp is skipped so the
    # window grows and the failed span is rescanned next run.
    if not DRY_RUN:
        try:
            _conn = _sq.connect(str(DB_PATH), timeout=5)
            _conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES('gmail_last_scan_at', ?)",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),),
            )
            _conn.commit()
            _conn.close()
        except Exception:
            pass  # window self-heals by growing; never crash the report

if __name__ == "__main__":
    run()
