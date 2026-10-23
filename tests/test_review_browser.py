"""Real Chromium review workflow. Install .[browser] and Chromium to run.

Set MBOX_REVIEW_BASE_URL=http://127.0.0.1:8765 to test the two-container stack.
Otherwise start a local test server/worker with synthetic temporary storage.
"""
import io
import os
import shutil
import threading
import zipfile
import uuid

import pytest
from pypdf import PdfReader

playwright = pytest.importorskip('playwright.sync_api', reason='optional browser test extra is not installed')
from pst2pdf.review import Store
from pst2pdf.review_server import make_server
from conftest import make_message, add_headers
from test_mbox import archive


@pytest.fixture
def browser_app(tmp_path):
    remote = os.environ.get('MBOX_REVIEW_BASE_URL')
    if remote:
        yield remote
        return
    root = tmp_path/'data'
    server=make_server(root,port=0)
    stop=threading.Event()
    def worker():
        store=Store(root)
        while not stop.is_set():
            if not store.work_once():stop.wait(.05)
    threads=[threading.Thread(target=server.serve_forever,daemon=True),threading.Thread(target=worker,daemon=True)]
    for thread in threads:thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    stop.set();server.shutdown();server.server_close()
    for thread in threads:thread.join(timeout=30)


def test_staff_review_and_generate_in_real_browser(tmp_path,browser_app):
    chromium=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:pytest.skip('Chromium is not installed; set CHROMIUM_PATH')
    a=make_message(subject='EXCLUDED-SECRET-SUBJECT',html='<p>Excluded body</p><img src="https://tracking.invalid/pixel"><script>alert(1)</script>',
                   attachments=[('shared.txt',b'SHARED-ATTACHMENT')])
    add_headers(a,Message_ID='<root@example.test>',From='Excluded Sender <excluded@example.test>',X_Gmail_Labels='Outside')
    b=make_message(subject='INCLUDED-ONE',html='<p>Included one body</p><img src="https://tracking.invalid/included">',plain='Included one body',attachments=[('shared.txt',b'SHARED-ATTACHMENT')])
    c=make_message(subject='INCLUDED-TWO',html=None,plain='Included two body',attachments=[('shared.txt',b'SHARED-ATTACHMENT')])
    add_headers(b,Message_ID='<one@example.test>');add_headers(c,Message_ID='<two@example.test>')
    messages=[a,b,c]+[make_message(subject=f'Other {i}',html=None,plain=f'Other body {i}') for i in range(30)]
    prefix=uuid.uuid4().hex
    paths=[archive(tmp_path/(prefix+'-browser-one.mbox'),messages),archive(tmp_path/(prefix+'-browser-two.mbox'),[a])]
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(executable_path=chromium,args=['--no-sandbox','--disable-dev-shm-usage'])
        context=browser.new_context(accept_downloads=True)
        page=context.new_page();requests=[]
        page.on('request',lambda r:requests.append(r.url))
        page.goto(browser_app)
        assert page.locator('[name=terms], [name=patterns], #attachment-csv').count()==0
        page.locator('#archives').set_input_files(paths)
        page.get_by_role('button',name='Import MBOX files',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 33 included · 0 excluded',timeout=30000)
        page.locator('[name=subject]').fill('Other')
        page.get_by_role('button',name='Apply filters',exact=True).click()
        playwright.expect(page.locator('#page-info')).to_contain_text('30 filtered results')
        page.get_by_role('button',name='Select this page',exact=True).click()
        playwright.expect(page.locator('#selection')).to_have_text('25 messages selected on this page')
        page.locator('#reason').fill('Page test reason')
        page.get_by_role('button',name='Exclude selected',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 8 included · 25 excluded')
        page.get_by_role('button',name='Undo last decision change',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 33 included · 0 excluded')
        page.get_by_role('button',name='Select all filtered results',exact=True).click()
        playwright.expect(page.locator('#selection')).to_have_text('All 30 filtered results selected (across all pages)')
        # Unapplied edits must not redefine the visible all-filtered selection.
        page.locator('[name=subject]').fill('INCLUDED')
        page.once('dialog',lambda d:d.accept())
        page.get_by_role('button',name='Exclude selected',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 3 included · 30 excluded')
        page.locator('[name=subject]').fill('EXCLUDED-SECRET')
        page.get_by_role('button',name='Apply filters',exact=True).click()
        page.get_by_role('button',name='EXCLUDED-SECRET-SUBJECT',exact=True).click()
        playwright.expect(page.locator('#reading')).to_contain_text('browser-one.mbox')
        playwright.expect(page.locator('#reading')).to_contain_text('browser-two.mbox')
        page.locator('#reason').fill('PRIVATE-REASON')
        page.get_by_role('button',name='Exclude this message',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 2 included · 31 excluded')
        page.get_by_role('button',name='Restore this message to Include',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 3 included · 30 excluded')
        page.get_by_role('button',name='Exclude this message',exact=True).click()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 2 included · 31 excluded')
        page.reload()
        playwright.expect(page.locator('#totals')).to_have_text('33 total · 2 included · 31 excluded',timeout=30000)
        page.get_by_role('button',name='Review generation totals',exact=True).click()
        playwright.expect(page.locator('#generation-summary')).to_have_text('33 total · 2 included · 31 excluded')
        page.get_by_role('button',name='Generate PDFs',exact=True).click()
        playwright.expect(page.get_by_role('link',name='Download review volume ZIP')).to_be_visible(timeout=90000)
        with page.expect_download() as download:
            page.get_by_role('link',name='Download review volume ZIP').click()
        text=''
        with zipfile.ZipFile(download.value.path()) as z:
            for name in z.namelist():
                assert name.startswith('Volume ') and name.endswith('.pdf')
                reader=PdfReader(io.BytesIO(z.read(name)))
                text+=' '.join(p.extract_text() for p in reader.pages)+str(reader.metadata)+str(reader.outline)
        assert 'Included one body' in text and 'SHARED-ATTACHMENT' in text
        for secret in ['EXCLUDED-SECRET-SUBJECT','Excluded body','excluded@example.test','PRIVATE-REASON','Other body']:
            assert secret not in text
        assert all(url.startswith(browser_app) for url in requests), requests
        page.screenshot(path=str(tmp_path/'review-interface.png'),full_page=True)
        context.close();browser.close()
