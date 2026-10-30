#!/usr/bin/env python3
"""Script-only outreach sync. Facts, not keyword guesses. No Gmail writes.

Rolling 30-day scan within parent/child-labeled application threads catches
unlabeled replies and alias-sent mail; ordinary research threads excluded.
Existing Gmail-linked history prevents duplicate actions. Unknown correspondents
need manual linking. Positive/closed outcomes never overwritten by reply detection.
"""
import argparse
import fcntl
import os
import re
import sqlite3
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {'not_contacted', 'contacted', 'no_response', 'needs_follow_up'}


def fetch(service, addresses, application_threads=None):
    messages = {}
    addresses = sorted(set(addresses))
    for i in range(0, len(addresses), 25):
        chunk = addresses[i:i+25]
        if any(not re.fullmatch(r'[^\s{}:"<>]+@[^\s{}:"<>]+', a) for a in chunk):
            raise ValueError('Invalid professor email; fix dashboard contact')
        query = 'newer_than:30d {' + ' '.join(f'from:{a} to:{a}' for a in chunk) + '}'
        page = None
        while True:
            result = service.users().messages().list(userId='me', q=query,
                          maxResults=500, pageToken=page).execute(num_retries=2)
            for m in result.get('messages', []):
                if m['id'] not in messages:
                    messages[m['id']] = service.users().messages().get(
                        userId='me', id=m['id'], format='metadata',
                        metadataHeaders=['From','To','Date','Auto-Submitted','Precedence']
                    ).execute(num_retries=2)
            page = result.get('nextPageToken')
            if not page:
                break
    return [m for m in messages.values()
            if application_threads is None or m.get("threadId") in application_threads]


def sync(conn, messages, dry=False):
    events = []
    for m in messages:
        if any(x in m.get('labelIds', []) for x in ('DRAFT','TRASH','SPAM')):
            continue
        h = {x['name'].lower(): x['value'] for x in m['payload']['headers']}
        if h.get('auto-submitted', 'no').lower() != 'no' or h.get('precedence','').lower() in ('bulk','list','junk'):
            continue
        when = parsedate_to_datetime(h['date'])
        if when.tzinfo is None:
            raise ValueError('Message Date lacks timezone: ' + m['id'])
        when = when.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        sent = 'SENT' in m.get('labelIds', [])
        addresses = [a.lower() for _, a in getaddresses([h.get('to' if sent else 'from','')])]
        events.extend((when, m['id'], sent, a) for a in addresses)
    lines = []
    conn.execute('BEGIN IMMEDIATE')
    try:
        for when, mid, sent, address in sorted(set(events)):
            matches = conn.execute('SELECT * FROM professors WHERE lower(email)=?', (address,)).fetchall()
            if len(matches) != 1:
                continue  # ponytail: exact unique email only; aliases require explicit linking.
            p = dict(matches[0])
            known = conn.execute('SELECT 1 FROM status_history WHERE professor_id=? AND gmail_message_id=?', (p['id'],mid)).fetchone()
            if known:
                continue
            status = p['status']
            last = p['last_contacted_at'] or ''
            action = p['next_action_at']
            newer = when > last
            if sent and newer:
                last = when
                if status in ACTIVE:
                    status, action = 'contacted', None
            elif not sent and newer and status in ACTIVE:
                status, action = 'needs_follow_up', when[:10]
            note = ('Outgoing mail detected.' if sent else
                    'New reply detected; review content. No acceptance/rejection inferred.')
            note += f' Message date: {when} UTC. https://mail.google.com/mail/u/0/#all/{mid}'
            conn.execute('UPDATE professors SET status=?,last_contacted_at=?,next_action_at=?,updated_at=? WHERE id=?',
                         (status,last or None,action,datetime.now(timezone.utc).isoformat(),p['id']))
            conn.execute('INSERT INTO status_history(professor_id,at,status,note,confirmed,rejected,gmail_message_id) VALUES(?,?,?,?,1,0,?)',
                         (p['id'],datetime.now(timezone.utc).isoformat(),status,note,mid))
            if newer:
                lines.append(f"{p['professor']}: {'sent date updated' if sent else 'reply needs review'} ({status})")
        if dry:
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return lines


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--dry-run', action='store_true')
    dry = args.parse_args().dry_run
    with open(ROOT / 'data' / '.gmail-status-sync.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import httplib2
        import google_auth_httplib2
        creds = Credentials.from_authorized_user_file(str(Path.home()/'.hermes/google_token.json'))
        service = build('gmail','v1', http=google_auth_httplib2.AuthorizedHttp(creds,http=httplib2.Http(timeout=25)), cache_discovery=False)
        conn = sqlite3.connect(os.environ.get('PROF_DB_PATH', ROOT/'data/evaluations.sqlite'), timeout=90)
        conn.row_factory = sqlite3.Row
        addresses = [r[0].strip().lower() for r in conn.execute("SELECT email FROM professors WHERE email IS NOT NULL AND trim(email) != ''")]
        base_row = conn.execute("SELECT value FROM config WHERE key='label_base'").fetchone()
        base = base_row[0] if base_row else '2026 applications'
        labels = service.users().labels().list(userId='me').execute(num_retries=2)['labels']
        label_ids = [x['id'] for x in labels if x['name']==base or x['name'].startswith(base+'/')]
        if not label_ids:
            raise RuntimeError('Application labels not found; refusing unscoped sync')
        threads = set()
        for lid in label_ids:
            page = None
            while True:
                result = service.users().messages().list(userId='me', labelIds=[lid],
                             maxResults=500, pageToken=page).execute(num_retries=2)
                threads.update(m['threadId'] for m in result.get('messages',[]))
                page = result.get('nextPageToken')
                if not page:
                    break
        messages = fetch(service, addresses, threads)  # Network completes before DB writes.
        lines = sync(conn, messages, dry)
        if lines:
            print(('DRY RUN\n' if dry else '') + 'Professor Gmail sync (no LLM)\n' + '\n'.join(lines))
        elif dry:
            print(f'DRY RUN: {len(messages)} messages checked; no new actions.')
        conn.close()


if __name__ == '__main__':
    main()
