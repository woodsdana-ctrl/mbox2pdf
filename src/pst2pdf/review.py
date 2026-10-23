"""Local review store and offline generation worker. No external services.

SQLite transactions protect decisions and immutable queued snapshots. No Python
objects are deserialized from disk. All archive reuse is verified by SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import cli, mbox, organize, render
from .model import BatesSpec, Options

INTERNAL = 'internal - DO NOT RELEASE'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with Path(path).open('rb') as fh:
        return hashlib.file_digest(fh, 'sha256').hexdigest()


def item_id(model):
    return hashlib.sha256(('review-v1|' + organize.dedup_key(model)).encode()).hexdigest()


def binding(manifest):
    records = sorted((a['name'], a['sha256']) for a in manifest)
    return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{64}', value):
        raise ValueError('Invalid identifier')
    return value


def counts(doc):
    included = sum(item['include'] for item in doc['items'])
    return dict(total=len(doc['items']), included=included, excluded=len(doc['items']) - included)


def filters_checked(filters):
    if not isinstance(filters, dict) or set(filters) - {'start', 'end', 'sender', 'recipient', 'subject', 'text', 'label', 'status'}:
        raise ValueError('Unknown filters')
    if any(not isinstance(v, str) or len(v) > 2000 for v in filters.values()):
        raise ValueError('Invalid filter')
    for key in ('start', 'end'):
        if filters.get(key):
            date.fromisoformat(filters[key])
    if filters.get('status', 'all') not in ('all', 'include', 'exclude'):
        raise ValueError('Invalid status filter')
    return filters


def matches(item, filters):
    filters_checked(filters)
    status = filters.get('status', 'all')
    if status != 'all' and item['include'] != (status == 'include'):
        return False
    for key in ('start', 'end'):
        bound = filters.get(key)
        if bound and (not item['date'] or (item['date'][:10] < bound if key == 'start' else item['date'][:10] > bound)):
            return False
    values = dict(sender=item['sender'], recipient=item['recipients'], subject=item['subject'],
                  text=item['subject'] + '\n' + item['body'], label='\n'.join(item['labels']))
    return all(not filters.get(key) or filters[key].casefold() in text.casefold() for key, text in values.items())


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.internal = self.root / INTERNAL
        self.sources = self.internal / 'sources'
        self.runs = self.internal / 'runs'
        for directory in (self.internal, self.sources, self.runs):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.internal / 'review.sqlite3'
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project TEXT NOT NULL,
                status TEXT NOT NULL, snapshot TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
            ''')
        self.db_path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def upload(self, name, stream, length):
        if not isinstance(name, str) or not name.lower().endswith('.mbox') or len(name) > 255 or '/' in name or '\\' in name:
            raise ValueError('Choose an .mbox file with a simple file name')
        if length < 0 or length > 20 * 1024**3:
            raise ValueError('Upload size must be at most 20 GiB; split larger exports')
        uid = uuid.uuid4().hex
        path = self.sources / (uid + '.mbox')
        try:
            with path.open('xb') as out:
                path.chmod(0o600)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024**2, remaining))
                    if not chunk:
                        raise ValueError('Upload interrupted; retry the import')
                    out.write(chunk)
                    remaining -= len(chunk)
            record = dict(id=uid, name=name, sha256=digest(path))
            with self.db() as db:
                db.execute('INSERT INTO uploads VALUES (?,?)', (uid, json.dumps(record)))
            return record
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def validate(self, doc):
        if binding(doc['archives']) != doc['id']:
            raise ValueError('Archive binding mismatch; import the original sources again')
        for a in doc['archives']:
            path = self.sources / (identifier(a['id']) + '.mbox')
            if digest(path) != a['sha256']:
                raise ValueError('Source archive hash mismatch; decisions cannot be reused')

    def get(self, pid, validate=False):
        with self.db() as db:
            row = db.execute('SELECT doc FROM projects WHERE id=?', (identifier(pid),)).fetchone()
        if row is None:
            raise ValueError('Review not found')
        doc = json.loads(row[0])
        if validate:
            self.validate(doc)
        return doc

    def projects(self):
        with self.db() as db:
            return [dict(id=d['id'], created=d['created'], names=[a['name'] for a in d['archives']], **counts(d))
                    for row in db.execute('SELECT doc FROM projects ORDER BY rowid DESC') for d in [json.loads(row[0])]]

    def read_models(self, doc, work, display_timezone):
        self.validate(doc)
        models = []
        for archive in doc['archives']:
            stats = defaultdict(int)
            path = self.sources / (identifier(archive['id']) + '.mbox')
            models.extend(mbox.read(path, work / archive['id'], stats, display_timezone))
            if stats['incomplete'] or stats['errors']:
                raise ValueError('Incomplete or unparseable archive; inspect the original export before review')
        self.validate(doc)
        return models

    def import_archives(self, ids, display_timezone='America/Chicago'):
        ZoneInfo(display_timezone)
        if not isinstance(ids, list) or not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise ValueError('Choose 1 to 100 distinct MBOX files')
        with self.db() as db:
            archives = []
            for uid in ids:
                row = db.execute('SELECT doc FROM uploads WHERE id=?', (identifier(uid),)).fetchone()
                if row is None:
                    raise ValueError('Uploaded file not found')
                archives.append(json.loads(row[0]))
        pid = binding(archives)
        doc = dict(id=pid, archives=archives, timezone=display_timezone, created=now(), revision=0, history=[], events=[], items=[])
        self.validate(doc)
        with self.db() as db:
            existing = db.execute('SELECT doc FROM projects WHERE id=?', (pid,)).fetchone()
        if existing:
            return self.get(pid, validate=True)
        with tempfile.TemporaryDirectory(prefix='review-import-', dir=self.internal) as tmp:
            models = self.read_models(doc, Path(tmp), display_timezone)
            entries = organize.organize(models, Options('Internal review', input_format='mbox'))
            names = {a['id']: a for a in archives}
            for entry in entries:
                m = entry.model
                associations = [dict(archive=names[Path(a['archive']).stem]['name'],
                                     sha256=names[Path(a['archive']).stem]['sha256'], archive_id=Path(a['archive']).stem,
                                     position=a['position'], labels=a['labels']) for a in m.source_associations]
                doc['items'].append(dict(id=item_id(m), date=m.date.isoformat() if m.date else '',
                    original_date=m.original_date, message_id=m.message_id, in_reply_to=m.in_reply_to,
                    references=m.references, sender=m.from_addr,
                    recipients=', '.join(v for v in [m.to_addrs, m.cc_addrs, m.bcc_addrs] if v),
                    to=m.to_addrs, cc=m.cc_addrs, bcc=m.bcc_addrs, subject=m.subject,
                    body=render.html_to_text(m.body_html), attachments=[a.filename for a in m.attachments],
                    labels=m.gmail_labels, sources=associations, issues=m.intake_issues, include=True, reason=''))
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO projects VALUES (?,?)', (pid, json.dumps(doc)))
        return self.get(pid, validate=True)

    def listing(self, pid, filters, page=1):
        doc = self.get(pid)
        filters_checked(filters)
        page = max(1, int(page))
        filtered = [i for i in doc['items'] if matches(i, filters)]
        page = min(page, max(1, (len(filtered) + 24) // 25))
        items = [{k: v for k, v in item.items() if k not in ('body', 'sources')} for item in filtered[(page-1)*25:page*25]]
        return dict(id=pid, revision=doc['revision'], counts=counts(doc), filtered=len(filtered), page=page, page_size=25,
                    items=items, timezone=doc['timezone'])

    def change(self, pid, revision, include=None, reason='', ids=None, filters=None, undo=False):
        doc = self.get(pid, validate=True)
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError('Reason must be at most 2000 characters')
        if not undo and type(include) is not bool:
            raise ValueError('Choose Include or Exclude')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            doc = json.loads(db.execute('SELECT doc FROM projects WHERE id=?', (pid,)).fetchone()[0])
            if revision != doc['revision']:
                raise ValueError('Review changed in another window; refresh before applying decisions')
            by_id = {i['id']: i for i in doc['items']}
            if undo:
                batch = next((b for b in reversed(doc['history']) if not b['undone']), None)
                if batch is None:
                    raise ValueError('No decision change to undo')
                for change in batch['changes']:
                    by_id[change['id']].update(change['before'])
                batch['undone'] = True
                doc['events'].append(dict(action='undo', batch=batch['id'], at=now()))
            else:
                if filters is not None and ids is not None:
                    raise ValueError('Choose explicit selection or all filtered results')
                if filters is not None:
                    targets = [i for i in doc['items'] if matches(i, filters)]
                else:
                    if not isinstance(ids, list) or not ids or any(i not in by_id for i in ids):
                        raise ValueError('Select existing messages')
                    targets = [by_id[i] for i in set(ids)]
                if not targets:
                    raise ValueError('No messages match this selection')
                changes = []
                for item in targets:
                    before = dict(include=item['include'], reason=item['reason'])
                    after = dict(include=include, reason=reason if not include else '')
                    if before != after:
                        changes.append(dict(id=item['id'], before=before, after=after))
                        item.update(after)
                if changes:
                    batch = dict(id=uuid.uuid4().hex, changes=changes, undone=False)
                    doc['history'].append(batch)
                    doc['events'].append(dict(action='decide', at=now(), **batch))
            doc['revision'] += 1
            db.execute('UPDATE projects SET doc=? WHERE id=?', (json.dumps(doc), pid))
        return dict(revision=doc['revision'], counts=counts(doc))

    def enqueue(self, pid, revision, settings):
        doc = self.get(pid, validate=True)
        if doc['revision'] != revision:
            raise ValueError('Decisions changed; review the generation totals again')
        if not counts(doc)['included']:
            raise ValueError('Include at least one message before generating PDFs')
        # Validate types and combinations before accepting the job.
        generation_options(settings)
        job = uuid.uuid4().hex
        snapshot = dict(project=doc, settings=settings, counts=counts(doc), queued_at=now())
        with self.db() as db:
            db.execute('INSERT INTO jobs (id,project,status,snapshot) VALUES (?,?,?,?)',
                       (job, pid, 'queued', json.dumps(snapshot)))
        return self.job(job)

    def job(self, uid):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier(uid),)).fetchone()
        if row is None:
            raise ValueError('Generation not found')
        snap = json.loads(row['snapshot'])
        return dict(id=uid, project=row['project'], status=row['status'], error=row['error'],
                    counts=snap['counts'], mode=snap['settings'].get('mode', 'review'),
                    queued_at=snap['queued_at'], output=f'{INTERNAL}/runs/{uid}/output')

    def jobs(self, pid):
        with self.db() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM jobs WHERE project=? ORDER BY rowid DESC', (identifier(pid),))]
        return [self.job(uid) for uid in ids]

    def work_once(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        uid = row['id']
        run = self.runs / uid
        run.mkdir(mode=0o700)
        snapshot = json.loads(row['snapshot'])
        (run / 'decisions - DO NOT RELEASE.json').write_text(json.dumps(snapshot, indent=2))
        try:
            doc = snapshot['project']
            options = generation_options(snapshot['settings'])
            self.validate(doc)
            with tempfile.TemporaryDirectory(prefix='work-', dir=run) as tmp:
                work = Path(tmp)
                models = self.read_models(doc, work / 'intake', options.display_timezone)
                expected = {i['id'] for i in doc['items']}
                actual = {item_id(m) for m in models}
                if actual != expected:
                    raise ValueError('Imported message fingerprints changed; reimport before generating')
                selected = {i['id'] for i in doc['items'] if i['include']}
                models = [m for m in models if item_id(m) in selected]
                stats = cli.build_volumes(self.sources, run / 'output', options, work / 'build', prepared_models=models)
                self.validate(doc)
                if stats['errors']:
                    raise ValueError('Generation has errors; output is incomplete and is internal only')
            if not options.triage and not options.survey:
                self.package(run)
            status, error = 'completed', ''
        except Exception as exc:
            status, error = 'failed', f'{type(exc).__name__}: {exc}'
            (run / 'INCOMPLETE - DO NOT RELEASE.txt').write_text(error)
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?, error=? WHERE id=?', (status, error, uid))
        return True

    @staticmethod
    def package(run):
        output = run / 'output'
        manifest = json.loads((output / 'manifest.json').read_text())
        volumes = manifest['volumes'] + manifest.get('appendix_volumes', [])
        with zipfile.ZipFile(run / 'review-volumes.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for volume in volumes:
                name = volume['filename']
                if not re.fullmatch(r'(?:Volume|Attachments) \d+ of \d+\.pdf', name):
                    raise ValueError('Unexpected output filename')
                if digest(output / name) != volume['sha256']:
                    raise ValueError('Output hash mismatch')
                archive.write(output / name, name)


def generation_options(settings):
    allowed = {'request_id', 'timezone', 'order', 'prefix', 'start', 'volume_pages', 'appendix_over', 'mode',
               'convert_attachments', 'dedup_attachments'}
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError('Unknown output options')
    def text(key, default='', limit=2000):
        value = settings.get(key, default)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'Invalid {key}')
        return value
    def integer(key, default, minimum, maximum):
        value = settings.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'Invalid {key}')
        return value
    mode = text('mode', 'review')
    if mode not in ('review', 'survey', 'triage'):
        raise ValueError('Invalid generation mode')
    zone = text('timezone', 'America/Chicago'); ZoneInfo(zone)
    order = text('order', 'thread')
    if order not in ('thread', 'chrono', 'folder'):
        raise ValueError('Invalid ordering')
    appendix = settings.get('appendix_over')
    if appendix is not None:
        integer('appendix_over', 0, 0, 100000)
    if mode == 'triage' and appendix is not None:
        raise ValueError('Triage does not convert attachments; clear the appendix threshold')
    for key in ('convert_attachments', 'dedup_attachments'):
        if key in settings and type(settings[key]) is not bool:
            raise ValueError('Invalid attachment option')
    options = Options(request_id=text('request_id', 'Request', 200) or 'Request', input_format='mbox',
        display_timezone=zone, order=order, bates=BatesSpec(prefix=text('prefix', 'DOC-', 40), start=integer('start', 1, 0, 10**9)),
        volume_pages=integer('volume_pages', 1000, 1, 100000), appendix_over=appendix,
        survey=mode == 'survey', triage=mode == 'triage', jobs=2,
        convert_attachments=settings.get('convert_attachments', True), dedup_attachments=settings.get('dedup_attachments', True))
    return options


def worker(root):
    os.umask(0o077)
    store = Store(root)
    with store.db() as db:
        db.execute("UPDATE jobs SET status='failed', error='Worker stopped during generation; generate a new run' WHERE status='running'")
    while True:
        if not store.work_once():
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Local MBOX review and offline conversion worker')
    parser.add_argument('mode', choices=['serve', 'worker'])
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.mode == 'worker':
        worker(args.data)
    else:
        from .review_server import serve
        serve(args.data, args.host, args.port)


if __name__ == '__main__':
    main()
