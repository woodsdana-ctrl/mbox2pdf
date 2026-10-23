"""Only synthetic data. Exercise persisted selection, source binding and real PDFs."""
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from pst2pdf.review import Store, counts, item_id, generation_options
from pst2pdf.review_server import make_server
from conftest import make_message, add_headers
from test_mbox import archive


def uploaded(store, path):
    with path.open('rb') as fh:
        return store.upload(path.name, fh, path.stat().st_size)['id']


def project(tmp_path, many=False):
    store=Store(tmp_path/'data')
    a=make_message(subject='EXCLUDED-SECRET-SUBJECT', html=None, plain='EXCLUDED-SECRET-BODY',
                   attachments=[('shared.txt',b'SHARED-ATTACHMENT-SENTINEL')])
    add_headers(a,Message_ID='<conflict@example.test>',From='Excluded Person <excluded@example.test>',X_Gmail_Labels='Outside scope')
    b=make_message(subject='INCLUDED-SUBJECT', html=None,plain='INCLUDED-BODY',
                   attachments=[('shared.txt',b'SHARED-ATTACHMENT-SENTINEL')])
    add_headers(b,Message_ID='<conflict@example.test>',X_Gmail_Labels='Inbox')
    c=make_message(subject='INCLUDED-REPLY',html=None,plain='Reply without private quoted text',
                   attachments=[('shared.txt',b'SHARED-ATTACHMENT-SENTINEL')])
    add_headers(c,Message_ID='<reply@example.test>',In_Reply_To='<conflict@example.test>')
    messages=[a,b,c]
    if many:
        messages += [make_message(subject=f'Other {i}',html=None,plain=f'Unique {i}') for i in range(30)]
    first=archive(tmp_path/'one.mbox',messages)
    second=archive(tmp_path/'two.mbox',[a])
    doc=store.import_archives([uploaded(store,first),uploaded(store,second)])
    return store,doc,first,second


def exclude_first(store,doc):
    bad=next(i for i in doc['items'] if i['subject']=='EXCLUDED-SECRET-SUBJECT')
    store.change(doc['id'],doc['revision'],False,'PRIVATE-REASON',[bad['id']])
    return bad


def test_stable_ids_duplicates_defaults_and_resume(tmp_path):
    store,doc,one,two=project(tmp_path)
    assert counts(doc)==dict(total=3,included=3,excluded=0)
    assert len({i['id'] for i in doc['items']})==3
    bad=exclude_first(store,doc)
    assert len(bad['sources'])==2 and {s['archive'] for s in bad['sources']}=={'one.mbox','two.mbox'}
    resumed=Store(store.root).get(doc['id'],validate=True)
    assert counts(resumed)['excluded']==1
    again=store.import_archives([uploaded(store,two),uploaded(store,one)])
    assert again['id']==doc['id']
    assert next(i for i in again['items'] if i['id']==bad['id'])['reason']=='PRIVATE-REASON'
    assert one.read_bytes()==(store.sources/(doc['archives'][0]['id']+'.mbox')).read_bytes()


def test_filtered_vs_page_bulk_and_undo(tmp_path):
    store,doc,*_=project(tmp_path,many=True)
    initial=store.listing(doc['id'],{},1)
    assert initial['filtered']==33 and len(initial['items'])==25
    assert store.listing(doc['id'],{'status':'exclude'})['filtered']==0
    assert counts(store.get(doc['id']))['included']==33  # filtering has no side effect
    store.change(doc['id'],0,False,'page only',[i['id'] for i in initial['items']])
    assert counts(store.get(doc['id']))['excluded']==25
    store.change(doc['id'],1,undo=True)
    assert counts(store.get(doc['id']))['excluded']==0
    store.change(doc['id'],2,False,'bulk',filters={'subject':'Other'})
    assert counts(store.get(doc['id']))['excluded']==30
    store.change(doc['id'],3,True,ids=[store.get(doc['id'])['items'][-1]['id']])
    assert counts(store.get(doc['id']))['excluded']==29
    store.change(doc['id'],4,undo=True)
    assert counts(store.get(doc['id']))['excluded']==30
    store.change(doc['id'],5,undo=True)
    assert counts(store.get(doc['id']))['included']==33
    with pytest.raises(ValueError,match='another window'):
        store.change(doc['id'],0,False,ids=[doc['items'][0]['id']])


def test_all_filters_and_missing_dates(tmp_path):
    store,doc,*_=project(tmp_path)
    pid=doc['id']
    assert store.listing(pid,{'label':'outside'})['filtered']==1
    assert store.listing(pid,{'sender':'excluded@'})['filtered']==1
    assert store.listing(pid,{'recipient':'bob@'})['filtered']==3
    assert store.listing(pid,{'text':'EXCLUDED-SECRET-BODY'})['filtered']==1
    assert store.listing(pid,{'start':'2025-01-01'})['filtered']==0
    assert store.listing(pid,{'end':'2023-01-01'})['filtered']==0
    assert store.listing(pid,{'start':'2024-03-15','end':'2024-03-15'})['filtered']==3
    with pytest.raises(ValueError):store.listing(pid,{'start':'not-a-date'})


def test_source_tampering_blocks_resume_decisions_and_generation(tmp_path):
    store,doc,*_=project(tmp_path)
    path=store.sources/(doc['archives'][0]['id']+'.mbox')
    path.write_bytes(path.read_bytes()+b'\nchanged\n')
    with pytest.raises(ValueError,match='hash mismatch'):store.get(doc['id'],validate=True)
    with pytest.raises(ValueError,match='hash mismatch'):store.change(doc['id'],0,False,ids=[doc['items'][0]['id']])
    with pytest.raises(ValueError,match='hash mismatch'):store.enqueue(doc['id'],0,{})


def test_invalid_archive_never_creates_review(tmp_path):
    store=Store(tmp_path/'data')
    uid=store.upload('bad.mbox',io.BytesIO(b'not mbox'),8)['id']
    with pytest.raises(ValueError,match='Incomplete'):store.import_archives([uid])
    assert not store.projects()
    with pytest.raises(ValueError):store.upload('../../escape.mbox',io.BytesIO(b'x'),1)
    with pytest.raises(ValueError):store.upload('truncated.mbox',io.BytesIO(b'x'),5)


def test_generation_excludes_content_recalculates_crossrefs_and_audits(tmp_path):
    store,doc,*_=project(tmp_path)
    bad=exclude_first(store,doc)
    doc=store.get(doc['id'])
    job=store.enqueue(doc['id'],doc['revision'],dict(request_id='Safe request',volume_pages=5,prefix='R-',start=21))
    # A later restoration must not alter the queued run snapshot.
    store.change(doc['id'],doc['revision'],True,ids=[bad['id']])
    assert store.work_once()
    assert store.job(job['id'])['status']=='completed',store.job(job['id'])
    run=store.runs/job['id']
    audit=json.loads((run/'decisions - DO NOT RELEASE.json').read_text())
    assert audit['counts']['excluded']==1
    assert any(i['reason']=='PRIVATE-REASON' for i in audit['project']['items'])
    out=run/'output';manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['counts']['messages']==2
    assert manifest['counts']['attachments_converted']==1 and manifest['counts']['attachments_crossref']==1
    labels=[];text=''
    with zipfile.ZipFile(run/'review-volumes.zip') as z:
        assert all(name.startswith('Volume ') and name.endswith('.pdf') for name in z.namelist())
        for name in z.namelist():
            r=PdfReader(io.BytesIO(z.read(name)));labels.extend(r.page_labels)
            text+=' '.join(p.extract_text() for p in r.pages)+str(r.metadata)+str(r.outline)
    assert labels==[f'R-{i:06d}' for i in range(21,21+len(labels))]
    assert 'INCLUDED-BODY' in text and 'SHARED-ATTACHMENT-SENTINEL' in text
    for secret in ('EXCLUDED-SECRET-SUBJECT','EXCLUDED-SECRET-BODY','excluded@example.test','PRIVATE-REASON','one.mbox','two.mbox'):
        assert secret not in text
    assert all(e['message']<=2 for e in manifest['exceptions'])
    assert all(m['message_id']!='' for m in manifest['messages'])


def test_changed_source_after_queue_fails_closed(tmp_path):
    store,doc,*_=project(tmp_path)
    job=store.enqueue(doc['id'],0,{})
    p=store.sources/(doc['archives'][0]['id']+'.mbox');p.write_bytes(b'changed')
    store.work_once()
    assert store.job(job['id'])['status']=='failed'
    assert not (store.runs/job['id']/'review-volumes.zip').exists()


def test_all_excluded_and_invalid_options_rejected(tmp_path):
    store,doc,*_=project(tmp_path)
    store.change(doc['id'],0,False,filters={})
    with pytest.raises(ValueError,match='at least one'):store.enqueue(doc['id'],1,{})
    for options in ({'volume_pages':0},{'start':'1'},{'mode':'triage','appendix_over':5},{'unknown':'x'},{'patterns':'unsafe'},{'terms':'Jane Example'},{'attachment_decisions':'filename,decision'}):
        with pytest.raises(ValueError):generation_options(options)


@pytest.fixture
def http_server(tmp_path):
    server=make_server(tmp_path/'data',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    yield base,server
    server.shutdown();server.server_close();thread.join()


def test_http_safety_and_inert_preview(http_server,tmp_path):
    base,server=http_server
    def request(path,body=None,headers=None):
        return urllib.request.urlopen(urllib.request.Request(base+path,data=body,headers=headers or {}))
    session=json.load(request('/api/session'))
    for headers in ({'Host':'evil.example'},{'Origin':'http://evil.example'},{'Sec-Fetch-Site':'cross-site'}):
        with pytest.raises(urllib.error.HTTPError):request('/api/session',headers=headers)
    navigation = {'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
    with request('/', headers=navigation) as response:
        assert response.status == 200 and 'text/html' in response.headers['Content-Type']
    for path in ('/api/session', '/app.js'):
        with pytest.raises(urllib.error.HTTPError):request(path, headers=navigation)
    with pytest.raises(urllib.error.HTTPError):request('/', headers={'Sec-Fetch-Site':'cross-site'})
    with pytest.raises(urllib.error.HTTPError):request('/api/import',b'{}',{'Content-Type':'application/json'})
    with request('/') as response:
        assert "frame-src 'none'" in response.headers['Content-Security-Policy']
        assert "img-src 'none'" in response.headers['Content-Security-Policy']
        assert response.headers['Cache-Control']=='no-store'
    msg=make_message(subject='<script>alert(1)</script>',html='<p>Safe text</p><script>BAD</script><img src="https://tracking.invalid/x"><img src="file:///etc/passwd">')
    path=archive(tmp_path/'malicious.mbox',[msg])
    headers={'Content-Type':'application/octet-stream','X-Review-Token':session['token']}
    upload=json.load(request('/api/upload?name=malicious.mbox',path.read_bytes(),headers))
    headers['Content-Type']='application/json'
    imported=json.load(request('/api/import',json.dumps({'uploads':[upload['id']]}).encode(),headers))
    from urllib.parse import urlencode
    listing=json.load(request('/api/messages?'+urlencode({'project':imported['id']})))
    item=json.load(request('/api/message?'+urlencode({'project':imported['id'],'id':listing['items'][0]['id']})))
    assert item['body']=='Safe text' and 'https://' not in item['body'] and '<script>' not in item['body']
    with pytest.raises(urllib.error.HTTPError):request('/../../etc/passwd')


def test_compose_network_boundary():
    text=(Path(__file__).resolve().parents[1]/'compose.yaml').read_text()
    assert '127.0.0.1:8765:8765' in text and 'network_mode: none' in text and 'driver: bridge' in text
    assert 'docker.sock' not in text and 'privileged:' not in text


@pytest.mark.parametrize('mode',['survey','triage','review'])
def test_worker_preserves_existing_output_modes(tmp_path,mode):
    store,doc,*_=project(tmp_path)
    exclude_first(store,doc);doc=store.get(doc['id'])
    settings=dict(mode=mode)
    job=store.enqueue(doc['id'],doc['revision'],settings)
    store.work_once();assert store.job(job['id'])['status']=='completed',store.job(job['id'])
    out=store.runs/job['id']/'output'
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['counts']['messages']==2
    assert not (store.runs/job['id']/'terms.txt').exists()
    if mode=='triage':
        assert (out/'Attachment index - DO NOT RELEASE.csv').exists()
        assert not (store.runs/job['id']/'review-volumes.zip').exists()
    elif mode=='survey':
        assert (out/'Attachment survey.pdf').exists()
        assert not list(out.glob('Volume *.pdf'))
    else:
        assert (store.runs/job['id']/'review-volumes.zip').exists()


def test_worker_appendix_with_selection_does_not_add_redactions(tmp_path):
    store,doc,*_=project(tmp_path);exclude_first(store,doc);doc=store.get(doc['id'])
    job=store.enqueue(doc['id'],doc['revision'],dict(appendix_over=0))
    store.work_once();assert store.job(job['id'])['status']=='completed',store.job(job['id'])
    out=store.runs/job['id']/'output'
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['counts']['appendix_items']==1
    with zipfile.ZipFile(store.runs/job['id']/'review-volumes.zip') as z:
        assert any(name.startswith('Attachments ') for name in z.namelist())
        annotations=[a.get_object().get('/Subtype') for name in z.namelist() for page in PdfReader(io.BytesIO(z.read(name))).pages for a in page.get('/Annots',[])]
    assert '/Redact' not in annotations


def test_empty_archive_and_quoted_excluded_text(tmp_path):
    store=Store(tmp_path/'data')
    empty=store.upload('empty.mbox',io.BytesIO(b''),0)
    assert counts(store.import_archives([empty['id']]))==dict(total=0,included=0,excluded=0)
    original=make_message(subject='EXCLUDED-ONLY-SUBJECT',html=None,plain='Original private quotation')
    reply=make_message(subject='Included reply',html=None,plain='Reply quotes: Original private quotation')
    path=archive(tmp_path/'quote.mbox',[original,reply])
    doc=store.import_archives([uploaded(store,path)])
    bad=next(i for i in doc['items'] if i['subject']=='EXCLUDED-ONLY-SUBJECT')
    store.change(doc['id'],0,False,ids=[bad['id']])
    job=store.enqueue(doc['id'],1,{})
    store.work_once();assert store.job(job['id'])['status']=='completed'
    text=' '.join(p.extract_text() for file in (store.runs/job['id']/'output').glob('Volume *.pdf') for p in PdfReader(file).pages)
    assert 'Original private quotation' in text  # selection is not rewriting/redaction
    assert 'EXCLUDED-ONLY-SUBJECT' not in text
