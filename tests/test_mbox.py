"""Synthetic offline Gmail exports; no real mail or review output is committed."""
from collections import defaultdict
from email import policy
from email.message import EmailMessage
import hashlib
import json
import mailbox
from pathlib import Path

import pytest
from pypdf import PdfReader

from pst2pdf import cli, mbox, organize, parse, render
from pst2pdf.model import DiskAttachment, Options
from conftest import add_headers, make_message, PNG_BYTES


def archive(path, messages):
    box = mailbox.mbox(path)
    for msg in messages:
        box.add(msg)
    box.close()
    return path


def read(path, tmp_path):
    st = defaultdict(int)
    return mbox.read(path, tmp_path / 'work', st, 'America/Chicago'), st


def test_single_multiple_labels_and_source_integrity(tmp_path):
    exports = tmp_path / 'exports'; exports.mkdir()
    a = make_message(); add_headers(a, Message_ID='<same@x>', X_Gmail_Labels='Inbox,"Project, A"')
    b = make_message(); add_headers(b, Message_ID='<same@x>', X_Gmail_Labels='Sent')
    p = archive(exports / 'a.mbox', [a]); q = archive(exports / 'b.MBOX', [b, make_message(subject='other')])
    before = {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in (p, q)}
    assert len(read(p, tmp_path)[0]) == 1
    models, st = read(exports, tmp_path)
    assert len(models) == 3 and not st['incomplete']
    entries = organize.organize(models, Options('test'))
    assert len(entries) == 2
    kept = next(e for e in entries if e.model.message_id == 'same@x')
    assert kept.copies == 2 and kept.folders == ['']
    assert kept.model.gmail_labels == ['Inbox', 'Project, A', 'Sent']
    assert [s['position'] for s in kept.model.source_associations] == [1, 1]
    assert kept.model.source_associations[0]['labels'] == ['Inbox', 'Project, A']
    assert all((f.read_bytes(), f.stat().st_mtime_ns) == before[f] for f in before)


@pytest.mark.parametrize('charset,cte', [('utf-8','base64'), ('iso-8859-1','quoted-printable')])
def test_mime_encodings_bcc_and_dates(tmp_path, charset, cte):
    msg = EmailMessage()
    msg['From'] = 'Renée Example <renee@example.test>'
    msg['To'] = 'staff@example.test'; msg['Cc'] = 'cc@example.test'; msg['Bcc'] = 'private@example.test'
    msg['Subject'] = 'Café résumé'; msg['Date'] = 'Fri, 15 Mar 2024 15:12:03 +0100'
    msg.set_content('Café\nFrom inside the body\n>From already quoted\n', charset=charset, cte=cte)
    models, _ = read(archive(tmp_path/'mail.mbox',[msg]), tmp_path)
    m = models[0]
    assert m.subject == 'Café résumé' and 'Renée' in m.from_addr
    assert 'Café' in m.body_html and 'From inside the body' in m.body_html
    assert m.bcc_addrs == 'private@example.test' and m.cc_addrs == 'cc@example.test'
    assert m.original_timestamp == '2024-03-15T15:12:03+01:00'
    assert m.date.hour == 9 and m.date.tzname() == 'CDT'
    assert 'private@example.test' in render.build_html(m)


def test_plain_from_line_and_crlf(tmp_path):
    path = tmp_path/'mail.mbox'
    path.write_bytes(b'From sender Fri Mar 15 09:00:00 2024\r\nFrom: a@example.test\r\n\r\nHello\r\n>From body\r\n')
    models, _ = read(path, tmp_path)
    assert len(models) == 1
    assert 'From body' in models[0].body_html and '&gt;From body' not in models[0].body_html


def test_alternatives_inline_attachments_and_attached_message(tmp_path):
    msg = make_message(html='<p>HTML body</p><img src="cid:picture"><img src="https://tracker.invalid/pixel">',
                       plain='Plain alternative', inline_png_cid='picture', attachments=[('../../plan.txt', b'plan')])
    nested = make_message(subject='Forwarded record', attachments=[('nested.txt', b'nested')])
    msg.add_attachment(nested, filename='forward.eml')
    models, _ = read(archive(tmp_path/'mail.mbox',[msg]), tmp_path)
    m = models[0]
    assert 'data:image/png;base64,' in m.body_html and 'HTML body' in m.body_html
    assert [a.filename for a in m.attachments] == ['../../plan.txt','forward.eml']
    assert all(isinstance(a, DiskAttachment) for a in m.attachments)
    assert 'Forwarded record' in m.attachments[1].content.decode()
    raw = m.source.read_bytes()
    assert b'Plain alternative' in raw
    forwarded = parse.parse_message(__import__('email.parser',fromlist=['BytesParser']).BytesParser(policy=policy.default).parsebytes(m.attachments[1].content), Path('nested.eml'))
    assert [a.filename for a in forwarded.attachments] == ['nested.txt']


@pytest.mark.parametrize('date', [None, 'nonsense', 'Fri, 15 Mar 2024 09:12:03 -0000'])
def test_missing_malformed_ambiguous_date(tmp_path, date):
    msg = make_message(date=None)
    path = archive(tmp_path/'mail.mbox',[msg])
    if date:
        raw = path.read_bytes()
        start = raw.index(b'\n') + 1
        path.write_bytes(raw[:start] + b'Date: ' + date.encode() + b'\n' + raw[start:])
    models, _ = read(path, tmp_path)
    assert models[0].intake_issues
    assert models[0].original_date == (date or '')


def test_dedup_conflicts_and_missing_id_attachments(tmp_path):
    a=make_message(attachments=[('a.txt',b'one')]); add_headers(a,Message_ID='<conflict@x>')
    b=make_message(attachments=[('a.txt',b'two')]); add_headers(b,Message_ID='<conflict@x>')
    c=make_message(attachments=[('a.txt',b'one')]); d=make_message(attachments=[('a.txt',b'two')])
    models,_=read(archive(tmp_path/'mail.mbox',[a,b,a,c,d,c]),tmp_path)
    entries=organize.organize(models,Options('test'))
    assert len(entries)==4
    conflicts=[e for e in entries if e.model.message_id]
    assert len(conflicts)==2 and all('Conflicting Message-ID' in ';'.join(e.model.intake_issues) for e in conflicts)
    assert len({e.thread_id for e in entries})==4  # same subject never merges MBOX


def test_reference_threading_ignores_outlook_and_subject_fallback(tmp_path):
    messages=[]
    for mid, reply in [('root',None),('reply','root'),('unrelated',None)]:
        msg=make_message();add_headers(msg, Message_ID=f'<{mid}@x>', In_Reply_To=f'<{reply}@x>' if reply else None,
                                     Thread_Index='A'*40, References='<root@x>' if reply else None)
        messages.append(msg)
    models,_=read(archive(tmp_path/'mail.mbox',messages),tmp_path)
    entries=organize.organize(models,Options('test'))
    ids={e.model.message_id:e.thread_id for e in entries}
    assert ids['root@x']==ids['reply@x']!=ids['unrelated@x']


def test_recoverable_failure_has_placeholder_and_next_message(tmp_path,monkeypatch):
    path=archive(tmp_path/'mail.mbox',[make_message(subject='bad'),make_message(subject='good')])
    original=parse.parse_message
    def fail(msg,*args,**kw):
        if str(msg['Subject'])=='bad': raise ValueError('synthetic failure')
        return original(msg,*args,**kw)
    monkeypatch.setattr(parse,'parse_message',fail)
    models,st=read(path,tmp_path)
    assert len(models)==2 and st['errors']==1
    assert 'could not be parsed' in models[0].subject and models[0].intake_issues
    assert models[1].subject=='good'


def test_archive_failure_marks_incomplete_and_retains_good_archive(tmp_path):
    exports=tmp_path/'exports';exports.mkdir()
    archive(exports/'good.mbox',[make_message()]);(exports/'bad.mbox').write_bytes(b'not an mbox')
    models,st=read(exports,tmp_path)
    assert len(models)==1 and st['incomplete'] and st['errors']==1
    assert st['intake_errors'] and st['intake_archives'][0]['complete'] is False


def test_parser_flags_and_pst_dispatch(tmp_path, monkeypatch):
    pst=tmp_path/'input.pst';pst.write_bytes(b'synthetic')
    seen=[]
    def extract(source, destination):
        seen.append(source);return destination
    monkeypatch.setattr(cli.extract,'extract_pst',extract)
    monkeypatch.setattr(cli,'build_volumes',lambda *a: {'errors':0})
    monkeypatch.setattr(cli,'_print_volume_summary',lambda st:None)
    assert cli.main([str(pst),str(tmp_path/'out')])==0
    assert seen==[pst]


def test_real_pdf_end_to_end(tmp_path):
    pytest.importorskip('weasyprint')
    exports=tmp_path/'exports';exports.mkdir()
    a=make_message(subject='Synthetic public records',plain='Searchable synthetic sentinel',html=None,
                   attachments=[('plan.txt',b'Synthetic attachment sentinel'),('unknown.bin', b'\x00\x01\x02')])
    add_headers(a, Message_ID='<root@example.test>',X_Gmail_Labels='Inbox')
    b=make_message(subject='Re: Synthetic public records',plain='Reply synthetic sentinel',html=None,
                   attachments=[('plan.txt',b'Synthetic attachment sentinel')])
    add_headers(b,Message_ID='<reply@example.test>',In_Reply_To='<root@example.test>')
    archive(exports/'one.mbox',[a,b]);archive(exports/'two.mbox',[a])
    out=tmp_path/'out';opts=Options('Request 2026-0001',jobs=1,input_format='mbox',display_timezone='America/Chicago',search_query='from:example.test')
    stats=cli.build_volumes(exports,out,opts,tmp_path/'build')
    assert stats['messages_found']==3 and stats['messages']==2 and stats['attachments']==3
    assert stats['attachments_crossref']==1 and stats['attachments_exceptions']==1
    volumes=sorted(out.glob('Volume *.pdf'));assert volumes
    readers=[PdfReader(p) for p in volumes]
    text='\n'.join(p.extract_text() for r in readers for p in r.pages)
    assert 'Searchable synthetic sentinel' in text and 'Synthetic attachment sentinel' in text
    assert 'cross-reference' in text.lower() or 'shown' in text.lower()
    assert readers[0].page_labels[0]=='DOC-000001'
    assert 'unknown.bin' in text
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['input']['format']=='mbox' and manifest['exceptions']
    assert len(manifest['messages'][0]['sources'])==2
    guide=' '.join(p.extract_text() for p in PdfReader(out/'00 START HERE.pdf').pages)
    assert 'MBOX' in guide and 'User-supplied search query' in guide and 'Purview' not in guide


def test_fatal_archive_cli_writes_incomplete_report(tmp_path):
    source=tmp_path/'bad.mbox';source.write_bytes(b'not mbox\n')
    out=tmp_path/'out'
    assert cli.main([str(source),str(out),'--jobs','1'])==1
    assert (out/'INCOMPLETE - DO NOT RELEASE.txt').is_file()
    doc=json.loads((out/'manifest.json').read_text())
    assert doc['input']['completeness']=='incomplete' and doc['input']['archive_errors']


def test_defective_mime_is_visible(tmp_path):
    p=tmp_path/'defective.mbox'
    p.write_bytes(b'From sender Fri Mar 15 09:00:00 2024\nFrom: a@example.test\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="missing"\n\nUnterminated multipart\n')
    models,st=read(p,tmp_path)
    assert len(models)==1 and models[0].intake_issues
    assert any('Boundary' in issue or 'Multipart' in issue for issue in models[0].intake_issues)


def test_spool_does_not_keep_all_attachment_bytes(tmp_path):
    messages=[make_message(attachments=[(f'{i}.txt',b'payload'*1000+str(i).encode())]) for i in range(30)]
    models,_=read(archive(tmp_path/'mail.mbox',messages),tmp_path)
    from dataclasses import fields
    for model in models:
        a=model.attachments[0]
        assert isinstance(a,DiskAttachment)
        assert 'content' not in {f.name for f in fields(a)}
        assert a.size==a.path.stat().st_size
        assert a.sha256==hashlib.sha256(a.content).hexdigest()


@pytest.mark.parametrize('mode', ['survey','triage'])
def test_mbox_survey_triage_reports_source(tmp_path,mode):
    source=archive(tmp_path/'mail.mbox',[make_message(attachments=[('test.txt',b'synthetic')])])
    out=tmp_path/'out'
    options=Options('test',jobs=1,input_format='mbox',**{mode:True})
    stats=cli.build_volumes(source,out,options,tmp_path/'build')
    assert stats['messages']==1
    assert json.loads((out/'Intake report - DO NOT RELEASE.json').read_text())['messages']
    if mode=='survey':
        text=' '.join(p.extract_text() for p in PdfReader(out/'Attachment survey.pdf').pages)
        assert 'MBOX' in text
    else:
        assert (out/'Attachment index - DO NOT RELEASE.csv').exists()
        assert list((out/'Original attachments - DO NOT RELEASE').glob('A*'))


def test_nested_message_display_timezone(tmp_path,monkeypatch):
    from pst2pdf.convert import Converter
    from conftest import make_model
    captured=[]
    model=make_model(date='2024-03-15T15:00:00+00:00')
    def rendered(m):
        captured.append(m.date)
        p=tmp_path/'made.pdf';p.write_bytes(b'synthetic');return p
    work=tmp_path/'nested';work.mkdir()
    with Converter(tmp_path/'convert',Options('test',jobs=1,display_timezone='America/Chicago'),rendered) as converter:
        converter._render_model(model,work)
    assert captured[0].hour==10 and captured[0].tzname()=='CDT'


def test_discovery_reports_unreadable_directory(tmp_path,monkeypatch):
    def failed(*a,**kw):
        kw['onerror'](PermissionError('synthetic unreadable directory'))
        yield
    monkeypatch.setattr(mbox.os,'walk',failed)
    with pytest.raises(ValueError,match='could not enumerate'):
        mbox.discover(tmp_path)


def test_mbox_does_not_rewrite_or_drop_readpst_named_attachments(tmp_path):
    ics=b'BEGIN:VCALENDAR\nPRODID:LibPST v0.6\nDTSTAMP:20240101T000000Z\nEND:VCALENDAR\n'
    msg=make_message(attachments=[('rtf-body.rtf',b'{\\rtf1 original}'),('i123.ics',ics)])
    models,_=read(archive(tmp_path/'mail.mbox',[msg]),tmp_path)
    assert [a.filename for a in models[0].attachments]==['rtf-body.rtf','i123.ics']
    assert models[0].attachments[1].content==ics


def test_intake_warning_is_not_reported_as_render_failure(tmp_path):
    source=archive(tmp_path/'mail.mbox',[make_message(date=None)])
    out=tmp_path/'out'
    stats=cli.build_volumes(source,out,Options('test',jobs=1,input_format='mbox',survey=True),tmp_path/'work')
    assert stats['render_failures']==[] and stats['intake_exceptions']==1
    text=' '.join(p.extract_text() for p in PdfReader(out/'Attachment survey.pdf').pages)
    assert 'intake exceptions' in text and 'only be shown as a header' not in text
    counts=json.loads((out/'manifest.json').read_text())['counts']
    assert counts['intake_exceptions']==1
