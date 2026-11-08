"""Contestant upload HTTP tests use only local servers and disposable synthetic data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import io
import json
import socket
import sqlite3
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit
import zipfile

from material_uploads import HTML_CSP, LIMITS
import test_submissions


PDF = b'%PDF-1.7\nSynthetic fixture\n%%EOF'
HTML = b'<!doctype html><title>Demo</title><script>document.body.dataset.ready="yes"</script><body>Demo</body>'


def archive(pptx=False):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as target:
        for name in (('[Content_Types].xml', 'ppt/presentation.xml') if pptx else ('main.py', 'README.md')):
            target.writestr(name, 'synthetic')
    return stream.getvalue()


class MaterialUploadTests(unittest.TestCase):
    tearDown = test_submissions.SubmissionAPITests.tearDown
    team = test_submissions.SubmissionAPITests.team
    access = test_submissions.SubmissionAPITests.access
    materials = test_submissions.SubmissionAPITests.materials
    open_submissions = test_submissions.SubmissionAPITests.open_submissions

    def setUp(self):
        test_submissions.SubmissionAPITests.setUp(self)
        # The public deployment uses HTTPS; the transport remains an isolated HTTP fixture.
        self.service.origin = 'https://event.example.test'
        self.team_id = self.team(name='测试队伍')
        self.client, _ = self.access(self.team_id)
        self.open_submissions()

    def upload(self, kind='slides', data=PDF, filename='original.pdf', project='Synthetic Project', client=None, headers=None, **kwargs):
        client = client or self.client
        return client.request('POST', '/api/team/uploads/' + kind, raw=data,
                              headers={'Content-Type': 'application/octet-stream', 'X-GOTC-Login': client.login_id,
                                       'X-GOTC-Project': quote(project, safe=''), 'X-GOTC-Filename': quote(filename, safe=''), **(headers or {})}, **kwargs)

    def uploaded(self, **kwargs):
        status, result, _ = self.upload(**kwargs)
        self.assertEqual(status, 200, result)
        return result

    def read(self, result, method='GET'):
        path = urlsplit(result['url']).path.removeprefix(self.service.base_path)
        return self.public.request(method, path)

    def submit(self, materials, version=0, request_id='material-submit-00001', status='submitted', client=None):
        return (client or self.client).post('/api/submission', {'expectedVersion': version, 'requestId': request_id, 'status': status, 'materials': materials})

    def rows(self):
        with self.service.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM material_uploads')]

    def test_names_binary_integrity_public_download_head_and_opaque_ids(self):
        result = self.uploaded(filename='../../arbitrary.exe.pdf', project='作品/../../标题+测试')
        self.assertEqual(result['filename'], '测试队伍+作品_.._.._标题_测试.pdf')
        self.assertEqual(result['size'], len(PDF))
        row = self.rows()[0]
        self.assertEqual(len(row['id']), 43)
        self.assertEqual(row['sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertEqual((self.service.material_uploads.root / row['id']).read_bytes(), PDF)
        status, content, headers = self.read(result)
        self.assertEqual((status, content), (200, PDF))
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        status, content, headers = self.read(result, 'HEAD')
        self.assertEqual((status, content, int(headers['Content-Length'])), (200, b'', len(PDF)))
        for path in ['/materials/', '/materials/' + row['id'] + '/wrong.pdf', '/materials/' + 'x' * 43 + '/wrong.pdf']:
            self.assertEqual(self.public.get(path)[0], 404)
        self.assertFalse(list(self.service.material_uploads.root.glob('.pending-*')))

    def test_unicode_name_limits_and_rejected_metadata(self):
        result = self.uploaded(project='中' * 140)
        self.assertLessEqual(len(result['filename'].encode()), 145)
        self.assertLess(len(result['url'].encode()), 1024)
        for headers in [{'X-GOTC-Project': '%ZZ'}, {'X-GOTC-Project': '%FF'}, {'X-GOTC-Project': '%0D%0Aevil'}, {'X-GOTC-Filename': 'x.html'}]:
            self.assertIn(self.upload(headers=headers)[0], {400, 415})
        self.assertEqual(self.upload(project='中' * 141)[0], 400)

    def test_html_sandbox_has_one_policy_and_keeps_inline_scripts_opaque(self):
        result = self.uploaded(kind='demo', data=HTML, filename='index.html')
        status, content, headers = self.read(result)
        self.assertEqual((status, content), (200, HTML))
        self.assertEqual(headers['Content-Security-Policy'], HTML_CSP)
        self.assertIn('sandbox allow-scripts;', HTML_CSP)
        self.assertNotIn('allow-same-origin', HTML_CSP)
        self.assertIn("script-src 'unsafe-inline' https:", HTML_CSP)
        self.assertIn("frame-ancestors 'none'", HTML_CSP)
        self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request('HEAD', urlsplit(result['url']).path)
        response = connection.getresponse()
        self.assertEqual(len(response.headers.get_all('Content-Security-Policy')), 1)
        response.read(); connection.close()
        self.assertNotIn('sandbox', self.public.get('/')[2]['Content-Security-Policy'])

    def test_auth_origin_marker_role_and_closed_window(self):
        for client, expected in [(self.public, 401), (self.admin, 403)]:
            self.assertEqual(self.upload(client=client)[0], expected)
        self.assertEqual(self.upload(origin=False)[0], 403)
        self.assertEqual(self.upload(origin='https://attacker.example')[0], 403)
        self.assertEqual(self.upload(headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.upload(headers={'X-GOTC-Login': ''})[0], 428)
        self.assertEqual(self.upload(headers={'X-GOTC-Login': 'stale'})[0], 401)
        self.assertEqual(self.admin.settings({'submissionsOpen': False})[0], 200)
        self.assertEqual(self.upload()[1]['code'], 'submission_closed')
        self.assertEqual(self.rows(), [])

    def test_size_kind_and_signature_validation(self):
        for kind, filename, data in [('slides', 'x.pptx', archive(True)), ('slides', 'x.ppt', bytes.fromhex('d0cf11e0a1b11ae1') + b'fixture'), ('files', 'x.zip', archive()), ('demo', 'x.htm', HTML)]:
            self.assertEqual(self.upload(kind, data, filename)[0], 200)
        for kind, filename, data in [('slides', 'x.pdf', HTML), ('slides', 'x.pptx', archive()), ('files', 'x.zip', b'PK\x03\x04bad'), ('demo', 'x.html', b'\xff\x00'), ('demo', 'x.html', b'plain text'), ('slides', 'x.exe', PDF)]:
            self.assertEqual(self.upload(kind, data, filename)[0], 415)
        self.assertEqual(self.upload(data=b'')[0], 413)
        for kind, limit in LIMITS.items():
            headers = {'Content-Length': str(limit + 1)}
            self.assertEqual(self.upload(kind=kind, data=b'', headers=headers)[0], 413)
        self.assertEqual(len(self.rows()), 4)
        self.assertFalse(list(self.service.material_uploads.root.glob('.pending-*')))

    def test_formal_receipt_history_replacement_and_link_checks(self):
        slides = self.uploaded()
        files = self.uploaded(kind='files', data=archive(), filename='code.zip')
        demo = self.uploaded(kind='demo', data=HTML, filename='index.html')
        materials = self.materials(slidesUrl=slides['url'], filesUrl=files['url'], demoUrl=demo['url'], videoUrl='')
        status, formal, _ = self.submit(materials)
        self.assertEqual(status, 200, formal)
        self.assertEqual(formal['receipt']['materials']['filesUrl'], files['url'])
        self.assertEqual(formal['submission']['submitted']['demoUrl'], demo['url'])
        self.assertEqual(self.client.get('/api/submission/history/1')[1]['materials']['slidesUrl'], slides['url'])
        replacement = self.uploaded(data=PDF + b' new')
        self.assertNotEqual(replacement['url'], slides['url'])
        self.assertEqual(self.submit({**materials, 'slidesUrl': replacement['url']}, 1, 'material-submit-00002')[0], 200)
        self.assertEqual(self.read(slides)[1], PDF)
        self.assertEqual(self.client.get('/api/submission/requests/material-submit-00001')[1]['materials']['slidesUrl'], slides['url'])
        checks = self.admin.get('/api/admin/submissions/' + self.team_id + '/checks?version=2')[1]['items']
        self.assertEqual(next(item['url'] for item in checks if item['field'] == 'filesUrl'), files['url'])
        csv = self.admin.get('/api/admin/submissions/export')[1].decode('utf-8-sig')
        self.assertIn('正式_filesUrl', csv)
        self.assertIn(files['url'], csv)
        for statement in ['UPDATE material_uploads SET filename=filename', 'DELETE FROM material_uploads']:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.service.db() as db: db.execute(statement)

    def test_cross_team_wrong_kind_unknown_link_and_project_rename_cannot_save(self):
        other, _ = self.access(self.team(2, '其他队伍'))
        slides = self.uploaded()
        cases = [(other, {'slidesUrl': slides['url']}, 403, 'upload_owner'),
                 (self.client, {'demoUrl': slides['url']}, 400, 'upload_kind'),
                 (self.client, {'slidesUrl': slides['url'], 'projectName': 'New project'}, 400, 'upload_project'),
                 (self.client, {'slidesUrl': slides['url'] + '?bad=1'}, 400, 'upload_reference'),
                 (self.client, {'slidesUrl': self.service.origin + self.service.base_path + '/materials/' + 'x' * 43 + '/bad.pdf'}, 400, 'upload_reference')]
        for number, (client, changes, expected, code) in enumerate(cases):
            status, body, _ = self.submit(self.materials(**changes), request_id='invalid-upload-' + str(number).zfill(5), status='draft', client=client)
            self.assertEqual((status, body['code']), (expected, code))
        self.assertEqual(self.client.get('/api/team')[1]['submission']['version'], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_internal_link_aliases_cannot_bypass_ownership_and_zip64_is_bounded(self):
        other, _ = self.access(self.team(2, 'Other'))
        slides = self.uploaded()
        aliases = [slides['url'].replace('event.example.test/', 'event.example.test:443/'),
                   slides['url'].replace('event.example.test/', 'event.example.test./'),
                   slides['url'].replace('/gotc/materials/', '/gotc/../gotc/materials/'),
                   slides['url'].replace('/gotc/materials/', '/gotc//materials/'),
                   slides['url'].replace('/gotc/materials/', '/gotc/%2e%2e/gotc/materials/')]
        for number, url in enumerate(aliases):
            status, _, _ = self.submit(self.materials(slidesUrl=url), request_id='alias-request-' + str(number).zfill(6), status='draft', client=other)
            self.assertEqual(status, 400)
        self.service.origin = 'https://8.141.114.201'
        numeric = self.uploaded()
        for number, host in enumerate(['8.141.114.201.', '0x8.0x8d.0x72.0xc9']):
            url = numeric['url'].replace('8.141.114.201/', host + '/')
            status, _, _ = self.submit(self.materials(slidesUrl=url), request_id='numeric-alias-' + str(number).zfill(6), status='draft', client=other)
            self.assertEqual(status, 400)
        ordinary = archive()
        end = ordinary.rfind(b'PK\x05\x06')
        # The legacy EOCD looks small, but a preceding ZIP64 locator must still be rejected.
        forged = ordinary[:end] + struct.pack('<4sLQL', b'PK\x06\x07', 0, 0, 1) + ordinary[end:]
        with patch('material_uploads.zipfile.ZipFile', side_effect=AssertionError('must reject before allocating central directory')):
            self.assertEqual(self.upload(kind='files', data=forged, filename='bad.zip')[0], 415)

    def test_only_slides_and_zip_are_required_and_receipt_history_preserve_optional_blanks(self):
        materials = {'slidesUrl': 'https://example.test/slides.pdf', 'filesUrl': 'https://example.test/work.zip', 'confirmed': False}
        for number, field in enumerate(('slidesUrl', 'filesUrl')):
            status, _, _ = self.submit({**materials, field: ''}, request_id='missing-required-' + str(number))
            self.assertEqual(status, 400)
        with self.service.db() as db:
            original_project = db.execute('SELECT project FROM teams WHERE id=?', (self.team_id,)).fetchone()[0]
        with patch.object(self.service.repositories, 'provider', side_effect=AssertionError('file-only must not contact a provider')):
            status, result, _ = self.submit(materials)
        self.assertEqual(status, 200, result)
        saved = result['receipt']['materials']
        for field in ('projectName', 'summary', 'codeUrl', 'codeVersion', 'demoUrl', 'license', 'sourcesAndLimitations'):
            self.assertEqual(saved[field], '')
        self.assertFalse(saved['confirmed'])
        self.assertEqual(result['team']['project'], original_project)
        self.assertIsNone(result['repositorySeal']); self.assertIsNone(result['repositoryBackup'])
        self.assertEqual(self.client.get('/api/submission/history/1')[1]['materials'], saved)
        self.assertEqual(self.submit(materials)[1]['receipt'], result['receipt'])

    def test_http_demo_and_long_share_urls_survive_draft_formal_receipt_and_history(self):
        base = {'slidesUrl': 'https://example.test/slides.pdf', 'filesUrl': 'https://example.test/work.zip'}
        prefix = 'https://share.example.test/view?token='
        urls = ['http://203.0.113.12:5173/demo/index.html?share=1#preview',
                prefix + 'a' * (8192 - len(prefix)),
                'https://share.example.test/view?title=' + '中' * 1500]
        version = 0
        for number, url in enumerate(urls):
            with self.subTest(url_number=number):
                materials = {**base, 'demoUrl': url}
                for status in ('draft', 'submitted'):
                    code, result, _ = self.submit(materials, version, f'demo-url-{number}-{status}', status)
                    self.assertEqual(code, 200, result)
                    version += 1
                    saved = result['submission']['draft' if status == 'draft' else 'submitted']
                    self.assertEqual(saved['demoUrl'], url)
                    if status == 'submitted':
                        self.assertEqual(result['receipt']['status'], 'succeeded')
                        self.assertEqual(result['receipt']['materials']['demoUrl'], url)
                        self.assertEqual(self.client.get('/api/submission/history/' + str(version))[1]['materials']['demoUrl'], url)

    def test_demo_url_expansion_keeps_scheme_content_byte_and_other_url_checks(self):
        base = {'slidesUrl': 'https://example.test/slides.pdf', 'filesUrl': 'https://example.test/work.zip'}
        invalid_demos = ['javascript:alert(1)', 'data:text/html,hello', '//example.test/demo',
                         'http://u:p@example.test/demo', 'https://example.test/a b',
                         'http://example.test/a\\b', 'http://example.test/%0aheader',
                         'http://example.test:0/demo', 'http://example.test:bad/demo',
                         'https://example.test/' + 'a' * 8192,
                         'https://example.test/' + '中' * 2730]
        changes = [{'demoUrl': url} for url in invalid_demos]
        changes += [{field: 'http://example.test/file'} for field in ('slidesUrl', 'filesUrl', 'codeUrl')]
        changes += [{field: 'https://example.test/' + 'a' * 1024} for field in ('slidesUrl', 'filesUrl', 'codeUrl')]
        for number, change in enumerate(changes):
            for status in ('draft', 'submitted'):
                with self.subTest(change=number, status=status):
                    code, _, _ = self.submit({**base, **change}, request_id=f'invalid-demo-{number}-{status}', status=status)
                    self.assertEqual(code, 400)
        self.assertEqual(self.client.get('/api/team')[1]['submission']['version'], 0)

    def test_http_aliases_of_internal_demo_uploads_cannot_bypass_canonical_or_owner_checks(self):
        other, _ = self.access(self.team(2, 'Other'))
        demo = self.uploaded(kind='demo', data=HTML, filename='index.html')
        base = {'slidesUrl': 'https://example.test/slides.pdf', 'filesUrl': 'https://example.test/work.zip'}
        aliases = [demo['url'].replace('https://', 'http://'),
                   demo['url'].replace('https://event.example.test/', 'http://EVENT.example.test.:80/'),
                   demo['url'].replace('https://event.example.test/', 'http://event.example.test:8080/'),
                   demo['url'].replace('https://', 'http://').replace('/gotc/materials/', '/gotc/%2e%2e/gotc/materials/')]
        for number, url in enumerate(aliases):
            for client_number, client in enumerate((self.client, other)):
                for status in ('draft', 'submitted'):
                    code, result, _ = self.submit({**base, 'demoUrl': url}, request_id=f'http-alias-{number}-{client_number}-{status}', status=status, client=client)
                    self.assertEqual((code, result.get('code')), (400, 'upload_reference'))
        # Canonical HTTPS still enforces ownership; a valid upload from this team works.
        code, result, _ = self.submit({**base, 'demoUrl': demo['url']}, client=other)
        self.assertEqual((code, result.get('code')), (403, 'upload_owner'))
        self.assertEqual(self.submit({**base, 'demoUrl': demo['url']})[0], 200)

    def test_uploaded_slides_zip_without_project_title_can_be_formally_submitted(self):
        slides = self.uploaded(project='')
        files = self.uploaded(kind='files', data=archive(), filename='code.zip', project='')
        self.assertEqual(slides['filename'], '测试队伍+作品.pdf')
        self.assertEqual(files['filename'], '测试队伍+作品.zip')
        status, result, _ = self.submit({'slidesUrl': slides['url'], 'filesUrl': files['url']})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['receipt']['materials']['projectName'], '')
        self.assertEqual(self.read(slides)[1], PDF)
        # A blank optional title also accepts previously named uploads without renaming bytes.
        named = self.uploaded(project='Named project')
        self.assertEqual(self.submit({'slidesUrl': named['url'], 'filesUrl': files['url']}, 1, 'named-upload-blank-title')[0], 200)

    def test_optional_filled_invalid_fields_still_reject_formal_submission(self):
        base = {'slidesUrl': 'https://example.test/slides', 'filesUrl': 'https://example.test/work.zip'}
        for number, change in enumerate([{'demoUrl': 'javascript:alert(1)'}, {'codeUrl': 'https://github.com/team/repo/blob/main/x'},
                                        {'codeVersion': 'main'}, {'confirmed': 'yes'}, {'summary': 'x' * 601}]):
            status, _, _ = self.submit({**base, **change}, request_id='optional-invalid-' + str(number))
            self.assertEqual(status, 400)

    def test_team_rename_requires_new_file_but_preserves_existing_receipts_and_bytes(self):
        slides = self.uploaded()
        self.assertEqual(self.submit(self.materials(slidesUrl=slides['url']))[0], 200)
        with self.service.db() as db:
            db.execute('UPDATE teams SET name=? WHERE id=?', ('新队名', self.team_id))
        status, result, _ = self.submit(self.materials(slidesUrl=slides['url']), 1, 'renamed-team-00001', status='draft')
        self.assertEqual((status, result['code']), (400, 'upload_project'))
        self.assertEqual(self.client.get('/api/submission/requests/material-submit-00001')[1]['materials']['slidesUrl'], slides['url'])
        self.assertEqual(self.read(slides)[1], PDF)
        renamed = self.uploaded()
        self.assertTrue(renamed['filename'].startswith('新队名+'))
        self.assertEqual(self.submit(self.materials(slidesUrl=renamed['url']), 1, 'renamed-team-00002')[0], 200)

    def test_quota_and_deadline_or_revocation_after_stream_do_not_publish(self):
        with patch.object(self.service.material_uploads, 'TEAM_QUOTA', len(PDF) - 1):
            self.assertEqual(self.upload()[1]['code'], 'upload_quota')
        original = self.service.material_uploads.validate_type
        def expire(path, extension):
            original(path, extension)
            with self.service.db() as db:
                db.execute("UPDATE settings SET value='1' WHERE key='submissionDeadline'")
        with patch.object(self.service.material_uploads, 'validate_type', side_effect=expire):
            self.assertEqual(self.upload()[1]['code'], 'submission_closed')
        self.open_submissions(submissionDeadline=None)
        def revoke(path, extension):
            original(path, extension)
            with self.service.db() as db:
                db.execute('DELETE FROM sessions WHERE account_id=(SELECT account_id FROM team_accounts WHERE team_id=?)', (self.team_id,))
        with patch.object(self.service.material_uploads, 'validate_type', side_effect=revoke):
            self.assertEqual(self.upload()[0], 401)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.service.material_uploads.root.iterdir()), [])

    def test_parallel_team_lock_and_quota_are_enforced_without_database_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.material_uploads.validate_type
        def held(path, extension):
            original(path, extension); entered.set(); self.assertTrue(release.wait(5))
        with patch.object(self.service.material_uploads, 'validate_type', side_effect=held), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.upload)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.upload()[1]['code'], 'upload_busy')
                self.assertEqual(self.admin.settings({'submissionsOpen': True})[0], 200)
            finally:
                release.set()
            self.assertEqual(first.result()[0], 200)
        with patch.object(self.service.material_uploads, 'TEAM_QUOTA', len(PDF)):
            self.assertEqual(self.upload()[1]['code'], 'upload_quota')

    def test_low_disk_space_retains_database_headroom_and_creates_no_file(self):
        boundary = self.service.material_uploads.MIN_FREE_BYTES + len(PDF)
        with patch('material_uploads.shutil.disk_usage', return_value=SimpleNamespace(free=boundary - 1)):
            status, result, _ = self.upload()
            self.assertEqual((status, result['code']), (507, 'storage_full'))
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.service.material_uploads.root.iterdir()), [])
        with patch('material_uploads.shutil.disk_usage', return_value=SimpleNamespace(free=boundary)):
            self.assertEqual(self.upload()[0], 200)

    def test_admin_scoped_workbench_upload_uses_selected_team(self):
        status, result, _ = self.admin.request('POST', '/api/admin/team-workbench', {'teamId': self.team_id}, headers={'X-GOTC-Login': self.admin.login_id})
        self.assertEqual(status, 200, result)
        self.admin.login_id = result['loginId']
        result = self.uploaded(client=self.admin)
        self.assertEqual(self.rows()[0]['team_id'], self.team_id)
        self.assertTrue(result['filename'].startswith('测试队伍+'))

    def test_incomplete_stream_cleanup_and_duplicate_length_rejection(self):
        headers = {'Host': 'localhost', 'Cookie': self.client.cookie, 'Origin': self.service.origin,
                   'X-GOTC-Login': self.client.login_id, 'X-GOTC-Project': 'Synthetic%20Project',
                   'X-GOTC-Filename': 'fixture.pdf', 'Content-Length': str(len(PDF) + 10)}
        request = ('POST ' + self.service.base_path + '/api/team/uploads/slides HTTP/1.1\r\n' +
                   ''.join(key + ': ' + value + '\r\n' for key, value in headers.items()) + '\r\n').encode() + PDF
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5) as connection:
            connection.sendall(request); connection.shutdown(socket.SHUT_WR)
            response = b''
            while chunk := connection.recv(65536): response += chunk
        self.assertIn(b'400 Bad Request', response)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.service.material_uploads.root.iterdir()), [])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.putrequest('POST', self.service.base_path + '/api/team/uploads/slides')
        for name, value in headers.items(): connection.putheader(name, value)
        connection.putheader('Content-Length', str(len(PDF))); connection.endheaders(PDF)
        response = connection.getresponse(); self.assertEqual(response.status, 400)
        response.read(); connection.close()


if __name__ == '__main__':
    unittest.main()
