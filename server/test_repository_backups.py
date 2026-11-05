"""Offline source backups: real Git/ZIP/bundle; synthetic local provider only."""
import os
from contextlib import closing
from pathlib import Path
import subprocess
import zipfile
import unittest
import json
import io
import time
from unittest.mock import patch
from test_api import Client

import test_repository_seals


class BackupTests(unittest.TestCase):
    fixture=test_repository_seals.SealedTests.fixture
    for _name in ('team','invitation','access','open_submissions','materials','request','apply','admin_action','preview','formal_submit','sealed_preview'):
        locals()[_name]=getattr(test_repository_seals.SealedTests,_name)
    def setUp(self):
        test_repository_seals.SealedTests.setUp(self)
        self.source=Path(self.service.data_dir).parent/'synthetic-source'
        self.source.mkdir()
        self.git('init','-q',str(self.source))
        self.git('-C',str(self.source),'config','user.email','synthetic@example.invalid')
        self.git('-C',str(self.source),'config','user.name','Synthetic')
        (self.source/'version.txt').write_text('A')
        (self.source/'.gitattributes').write_text('ignored.txt export-ignore\nversion.txt export-subst\n')
        (self.source/'ignored.txt').write_text('included')
        self.git('-C',str(self.source),'add','.')
        self.git('-C',str(self.source),'commit','-qm','A')
        self.sha_a=self.git('-C',str(self.source),'rev-parse','HEAD').strip()
        self.remote.sha=self.sha_a
        self.backups=self.service.repository_backups
        original=self.remote
        def provider(method,path,body=None):
            if path=='/user':return 200,{'login':'Synthetic'}
            return original(method,path,body)
        self.repos.providers['gitee'].request=provider
        def local_fetch(repo,source,env,ctx,provider):
            self.backups._git(repo,['-c','protocol.file.allow=always','fetch','--no-tags','--no-recurse-submodules',str(self.source),'+HEAD:refs/heads/fetched'],ctx,env)
        self.backups.fetch_source=local_fetch

    def git(self,*args):
        return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL).decode()

    def enqueue_selected(self,sha):
        self.remote.sha=sha
        self.selected=self.sealed_preview()
        status,self.result,_=self.formal_submit(self.selected)
        self.assertEqual(status,200,self.result)
        self.assertEqual(self.result['repositoryBackup']['status'],'queued')

    def advance_source_to(self,label='B'):
        (self.source/'version.txt').write_text(label)
        self.git('-C',str(self.source),'commit','-qam',label)
        self.sha_b=self.git('-C',str(self.source),'rev-parse','HEAD').strip()

    def record(self):
        with self.service.db() as db:return self.backups.for_version(db,self.team_id,1)

    def read_zip_file(self,record,name):
        status,data,_=self.client.get(record['downloadUrl'][len(self.service.base_path):])
        self.assertEqual(status,200,data)
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:return archive.read(name).decode()

    def test_backup_preserves_selected_sha_when_source_head_advanced(self):
        self.enqueue_selected(self.sha_a)
        self.advance_source_to()
        self.backups.run_once()
        record=self.record()
        self.assertEqual(record['status'],'ready',record)
        self.assertEqual(record['sha'],self.sha_a)
        self.assertEqual(self.read_zip_file(record,'version.txt'),'A')
        self.assertEqual(self.read_zip_file(record,'ignored.txt'),'included')

    def commit_source(self):
        self.git('-C',str(self.source),'add','.')
        self.git('-C',str(self.source),'commit','-qm','fixture')
        self.sha_a=self.git('-C',str(self.source),'rev-parse','HEAD').strip()

    def test_bundle_verifies_and_restores_without_original_repository(self):
        self.enqueue_selected(self.sha_a)
        self.backups.run_once()
        record=self.record()
        status,data,headers=self.client.get(record['bundleUrl'][len(self.service.base_path):])
        self.assertEqual(status,200)
        bundle=self.source.parent/'download.bundle';bundle.write_bytes(data)
        restored=self.source.parent/'restored'
        self.git('clone','-q',str(bundle),str(restored))
        self.assertEqual(self.git('-C',str(restored),'rev-parse','HEAD').strip(),self.sha_a)
        self.assertEqual((restored/'version.txt').read_text(),'A')
        self.assertEqual(headers['Content-Type'],'application/octet-stream')
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')

    def test_lfs_and_submodule_are_explicitly_partial(self):
        (self.source/'large.bin').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:'+'0'*64+'\nsize 4096\n')
        self.git('-C',str(self.source),'add','large.bin')
        self.git('-C',str(self.source),'update-index','--add','--cacheinfo','160000,'+self.sha_a+',external')
        self.git('-C',str(self.source),'commit','-qm','partial assets')
        self.sha_a=self.git('-C',str(self.source),'rev-parse','HEAD').strip()
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        record=self.record()
        self.assertEqual(record['status'],'partial',record)
        self.assertEqual(record['missingAssets'],[{'path':'external','kind':'submodule'},{'path':'large.bin','kind':'lfs'}])
        self.assertTrue(record['downloadUrl'])

    def test_unsafe_paths_and_symlinks_never_publish(self):
        (self.source/'unsafe\\name').write_text('unsafe')
        self.commit_source();self.enqueue_selected(self.sha_a);self.backups.run_once()
        self.assertEqual(self.record()['error'],'unsafe_path')
        self.assertIsNone(self.record()['downloadUrl'])

    def test_selected_sha_missing_is_never_replaced(self):
        self.enqueue_selected('b'*40);self.backups.run_once()
        record=self.record()
        self.assertEqual(record['sha'],'b'*40)
        self.assertEqual(record['status'],'queued')
        self.assertIsNone(record['downloadUrl'])

    def test_retry_delays_attempt_bound_and_error_redaction(self):
        self.enqueue_selected(self.sha_a)
        now=[1000.0];self.backups.clock=lambda:now[0]
        def fail(*args):raise RuntimeError('synthetic-provider-secret https://private.invalid')
        self.backups.fetch_source=fail
        for attempt,delay in ((1,60),(2,300),(3,0)):
            self.assertTrue(self.backups.run_once())
            record=self.record();self.assertEqual(record['attempts'],attempt)
            self.assertEqual(record['error'],'backup_failed')
            if delay:
                self.assertFalse(self.backups.run_once());now[0]+=delay-1
                self.assertFalse(self.backups.run_once());now[0]+=1
        self.assertEqual(self.record()['status'],'failed')
        self.assertFalse(self.backups.run_once())
        self.assertNotIn('secret',json.dumps(self.record()))

    def test_job_dedup_restart_and_original_receipt_replay(self):
        self.enqueue_selected(self.sha_a)
        receipt=self.result['receipt']
        with self.service.db() as db:
            self.backups.enqueue(db,self.team_id,1,receipt['requestId'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_backups').fetchone()[0],1)
            db.execute("UPDATE repository_backups SET status='running',attempts=1")
        with self.service.db() as db:self.backups.initialize(db)
        self.assertEqual(self.record()['status'],'queued')
        self.backups.run_once()
        result=self.formal_submit(self.selected)[1]
        self.assertTrue(result['replayed']);self.assertEqual(result['receipt'],receipt)
        self.assertEqual(result['repositoryBackup']['status'],'ready')
        detail=self.request(self.client,'/api/submission/history/1')[1]
        self.assertEqual(detail['repositoryBackup'],result['repositoryBackup'])

    def test_limits_are_enforced_during_work(self):
        self.enqueue_selected(self.sha_a)
        now=[1000.0];self.backups.clock=lambda:now[0]
        for attribute,value,error in [('SOURCE_BYTES',1,'source_limit'),('FILE_COUNT',1,'file_count_limit'),('TASK_BYTES',1,'task_limit')]:
            with self.subTest(limit=attribute),patch.object(self.backups,attribute,value):
                self.backups.run_once()
                self.assertEqual(self.record()['error'],error,self.record())
                self.assertIsNone(self.record()['downloadUrl'])
            now[0]+=400

    def test_storage_cap_and_git_unavailable(self):
        self.enqueue_selected(self.sha_a)
        with patch.object(self.backups,'TOTAL_BYTES',1):self.backups.run_once()
        self.assertEqual(self.record()['error'],'storage_limit')
        self.backups.clock=lambda:time.time()+400
        with patch.object(self.backups,'git_binary',None):self.backups.run_once()
        self.assertEqual(self.record()['error'],'git_unavailable')

    def test_git_wall_clock_timeout_and_shutdown_kill(self):
        self.enqueue_selected(self.sha_a)
        def slow(repo,source,env,ctx,provider):
            self.backups._git(repo,['-c','alias.wait=!sleep 10','wait'],ctx)
        self.backups.fetch_source=slow
        start=time.monotonic()
        with patch.object(self.backups,'GIT_SECONDS',.15):self.backups.run_once()
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(self.record()['error'],'git_timeout')
        self.assertIsNone(self.backups.process)

    def test_private_download_authorization_and_streaming_value(self):
        from repository_backups import ArchiveDownload
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        route=self.record()['downloadUrl'][len(self.service.base_path):]
        self.assertEqual(Client(self.server).get(route)[0],401)
        other,_=self.access(self.team(number=2))
        self.assertEqual(other.get(route)[0],403)
        self.assertEqual(self.admin.get(route)[0],200)
        with self.service.db() as db:
            judge,code=self.service.create_account(db,'judge','Synthetic Judge')
        client=Client(self.server);client.post('/api/login',{'role':'judge','code':code})
        self.assertEqual(client.get(route)[0],403)
        value,_,_=self.service.dispatch('GET',route,{},self.client.cookie,'127.0.0.1')
        self.assertIsInstance(value,ArchiveDownload)
        self.assertFalse(isinstance(value.stream,bytes));value.stream.close()
        status,data,headers=self.client.get(route)
        self.assertEqual(status,200)
        self.assertEqual(headers['Content-Type'],'application/zip')
        self.assertEqual(headers['Content-Disposition'],'attachment; filename="source.zip"')
        self.assertNotIn('token',route)

    def test_artifact_corruption_or_symlink_hides_downloads(self):
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        route=self.record()['downloadUrl'][len(self.service.base_path):]
        with self.service.db() as db:
            artifact_id=db.execute('SELECT artifact_id FROM repository_backups').fetchone()[0]
        archive=self.backups.root/'artifacts'/artifact_id/'source.zip'
        archive.unlink();archive.symlink_to(self.source/'version.txt')
        self.assertEqual(self.record()['status'],'failed')
        self.assertIsNone(self.record()['downloadUrl'])
        self.assertEqual(self.client.get(route)[0],404)

    def test_bound_numeric_identity_checked_outside_database_writer(self):
        self.enqueue_selected(self.sha_a)
        self.remote.repo['id']=999
        self.backups.run_once()
        self.assertEqual(self.record()['status'],'queued')
        self.assertIsNone(self.record()['downloadUrl'])

    def test_no_threads_on_construction_and_opt_in_is_exact(self):
        self.assertIsNone(self.backups.thread)
        with patch.dict(os.environ,{'GOTC_REPO_BACKUP_ENABLED':'true'}):self.backups.start()
        self.assertIsNone(self.backups.thread)
        self.enqueue_selected(self.sha_a)
        def fail(*args):raise RuntimeError('synthetic secret')
        self.backups.fetch_source=fail
        with patch.dict(os.environ,{'GOTC_REPO_BACKUP_ENABLED':'1'}):self.backups.start()
        self.backups.stop()
        self.assertFalse(self.backups.thread.is_alive())

    def test_production_fetch_uses_only_fixed_origin_and_child_credentials(self):
        from repository_backups import RepositoryBackups
        self.enqueue_selected(self.sha_a)
        provider=self.repos.providers['gitee'];provider.token='synthetic-never-real-secret'
        original_git=self.backups._git
        captured=[]
        def intercepted(repo,args,ctx,env=None,input_path=None):
            if args[0]=='fetch':
                captured.append((args,dict(env)))
                self.assertTrue(Path(env['GIT_ASKPASS']).is_file())
                return original_git(repo,['-c','protocol.file.allow=always','fetch','--no-tags','--no-recurse-submodules',str(self.source),'+HEAD:refs/heads/fetched'],ctx)
            return original_git(repo,args,ctx,env,input_path)
        self.backups._git=intercepted
        self.backups.fetch_source=lambda *args:RepositoryBackups.fetch_source(self.backups,*args)
        with patch.dict(os.environ,{'UNRELATED_PROVIDER_SECRET':'never-inherit'}):self.backups.run_once()
        record=self.record();self.assertEqual(record['status'],'ready',record)
        args,env=captured[0]
        self.assertTrue(args[-2].startswith('https://gitee.com/'))
        self.assertEqual(env['GOTC_GIT_TOKEN'],provider.token)
        self.assertNotIn(provider.token,' '.join(args))
        self.assertNotIn('UNRELATED_PROVIDER_SECRET',env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'],os.devnull)
        self.assertFalse(Path(env['GIT_ASKPASS']).exists())
        for path in (self.backups.root/'artifacts').rglob('*'):
            if path.is_file():self.assertNotIn(provider.token.encode(),path.read_bytes())

    def test_shutdown_interrupts_active_git_process_group(self):
        import threading
        self.enqueue_selected(self.sha_a)
        def slow(repo,source,env,ctx,provider):self.backups._git(repo,['-c','alias.wait=!sleep 20','wait'],ctx)
        self.backups.fetch_source=slow
        thread=threading.Thread(target=self.backups.run_once);thread.start()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.backups.process_lock:
                process=self.backups.process
                active=process is not None and 'alias.wait' in ' '.join(process.args)
            if active:break
            time.sleep(.01)
        self.assertTrue(active)
        self.backups.stop();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.record()['error'],'worker_interrupted')

    def test_disk_pressure_stops_work_before_publication(self):
        from collections import namedtuple
        Usage=namedtuple('Usage','total used free')
        self.enqueue_selected(self.sha_a)
        with patch('repository_backups.shutil.disk_usage',return_value=Usage(100,100,0)):self.backups.run_once()
        self.assertEqual(self.record()['error'],'storage_limit')
        self.assertIsNone(self.record()['downloadUrl'])

    def test_unsafe_symlink_target_is_rejected_without_unpacking(self):
        (self.source/'escape').symlink_to('../../outside')
        self.commit_source();self.enqueue_selected(self.sha_a);self.backups.run_once()
        self.assertEqual(self.record()['error'],'unsafe_path')
        self.assertFalse((self.backups.root/'outside').exists())

    def test_recovery_respects_attempt_limit(self):
        self.enqueue_selected(self.sha_a)
        with self.service.db() as db:
            db.execute("UPDATE repository_backups SET status='running',attempts=3")
        with self.service.db() as db:self.backups.initialize(db)
        self.assertEqual(self.record()['status'],'failed')
        self.assertFalse(self.backups.run_once())

    def test_same_sealed_source_reuses_verified_artifacts_without_fetch(self):
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        first=self.record()
        preview=self.sealed_preview()
        self.remote.fail_all=True
        status,result,_=self.formal_submit(preview,version=1,request_id='second-backup-version',materials=self.materials(codeUrl='',codeVersion='',summary='Updated summary'))
        self.assertEqual(status,200,result)
        self.assertEqual(result['repositoryBackup']['status'],'queued')
        def forbidden(*args):raise AssertionError('repeated fetch forbidden')
        self.backups.fetch_source=forbidden
        self.backups.run_once()
        with self.service.db() as db:
            second=self.backups.for_version(db,self.team_id,2)
            ids=[r[0] for r in db.execute('SELECT artifact_id FROM repository_backups ORDER BY version')]
        self.assertEqual(second['status'],'ready',second)
        self.assertEqual(first['bundleSha256'],second['bundleSha256'])
        self.assertEqual(ids[0],ids[1])

    def test_unsealed_submissions_have_no_backup_job(self):
        self.repos.seals.enabled=False
        status,preview,_=self.preview()
        self.assertEqual(status,200,preview)
        status,result,_=self.formal_submit(preview)
        self.assertEqual(status,200,result)
        self.assertIsNone(result['repositoryBackup'])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM repository_backups').fetchone()[0],0)

    def test_queued_backup_never_downloads_during_preview_confirmation(self):
        def forbidden(*args):raise AssertionError('confirmation must not download')
        self.backups.fetch_source=forbidden
        self.enqueue_selected(self.sha_a)
        path='/api/submission/backups/'+self.team_id+'/1/source.zip'
        self.assertEqual(self.client.get(path)[0],404)
        self.assertIsNone(self.record()['downloadUrl'])
        self.assertEqual(self.record()['attempts'],0)

    def test_mid_process_disk_growth_is_terminated_and_removed(self):
        self.enqueue_selected(self.sha_a)
        def grow(repo,source,env,ctx,provider):
            self.backups._git(repo,['-c','alias.grow=!dd if=/dev/zero of=growth bs=1024 count=10000 2>/dev/null','grow'],ctx)
        self.backups.fetch_source=grow
        with patch.object(self.backups,'TASK_BYTES',65536):self.backups.run_once()
        self.assertEqual(self.record()['error'],'task_limit')
        self.assertIsNone(self.record()['downloadUrl'])
        self.assertEqual(list((self.backups.root/'scratch').iterdir()),[])
        self.assertIsNone(self.backups.process)

    def test_main_handles_sigterm_and_stops_worker(self):
        import signal
        from unittest.mock import Mock
        import app
        service=Mock(base_path='/gotc')
        server=Mock(server_port=1234)
        def terminated():raise KeyboardInterrupt()
        server.serve_forever.side_effect=terminated
        with patch.object(app,'EventService',return_value=service),patch.object(app,'EventHTTPServer',return_value=server),patch.object(app,'print'),patch('signal.signal') as install:
            app.main()
        self.assertEqual(install.call_args_list[0].args[0],signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):install.call_args_list[0].args[1](signal.SIGTERM,None)
        service.repository_backups.start.assert_called_once()
        service.repository_backups.stop.assert_called_once()
        server.server_close.assert_called_once()

    def test_ready_replay_and_draft_metadata_do_not_read_artifacts_under_writer(self):
        import sqlite3
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        receipt=self.result['receipt']
        artifact_opens=[];writer_held=[]
        original_open=os.open
        def observed_open(path,*args,**kwargs):
            if Path(path).is_relative_to(self.backups.root/'artifacts'):
                artifact_opens.append(str(path))
                with closing(sqlite3.connect(self.service.db_path,timeout=.01)) as other:
                    try:other.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError:writer_held.append(True)
                    finally:other.rollback()
            return original_open(path,*args,**kwargs)
        with patch('repository_backups.os.open',side_effect=observed_open),patch.object(self.backups,'open_verified',wraps=self.backups.open_verified) as verify:
            replay=self.formal_submit(self.selected)[1]
            self.assertTrue(replay['replayed']);self.assertEqual(replay['receipt'],receipt)
            self.assertEqual(replay['repositoryBackup']['status'],'ready')
            status,draft,_=self.request(self.client,'/api/submission',{'requestId':'ready-draft-metadata','expectedVersion':1,'status':'draft','materials':{**receipt['materials'],'summary':'Draft retaining ready formal version'}})
            self.assertEqual(status,200,draft)
            self.assertEqual(draft['repositoryBackup']['status'],'ready')
            self.assertEqual(self.client.get('/api/team')[1]['repositoryBackup']['status'],'ready')
            self.assertEqual(self.request(self.client,'/api/submission/history/1')[1]['repositoryBackup']['status'],'ready')
            self.assertEqual(verify.call_count,0,'Metadata must not invoke full artifact verification')
        self.assertEqual(artifact_opens,[],'Responses must not open artifact contents')
        self.assertEqual(writer_held,[],'No artifact reads may occur under the submission writer')
        with closing(sqlite3.connect(self.service.db_path,timeout=.01)) as other:
            other.execute('BEGIN IMMEDIATE');other.rollback()

    def test_same_size_corruption_is_rejected_on_download_and_worker_reuse(self):
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        route=self.record()['downloadUrl'][len(self.service.base_path):]
        with self.service.db() as db:artifact_id=db.execute('SELECT artifact_id FROM repository_backups').fetchone()[0]
        archive=self.backups.root/'artifacts'/artifact_id/'source.zip'
        with archive.open('r+b') as stream:
            byte=stream.read(1);stream.seek(0);stream.write(bytes([byte[0]^1]))
        self.assertEqual(self.client.get(route)[0],404)
        preview=self.sealed_preview()
        status,result,_=self.formal_submit(preview,version=1,request_id='corrupt-reuse-version',materials=self.materials(codeUrl='',codeVersion='',summary='Updated summary'))
        self.assertEqual(status,200,result)
        self.remote.fail_all=True
        self.backups.run_once()
        with self.service.db() as db:
            second=self.backups.for_version(db,self.team_id,2)
            artifact=db.execute('SELECT artifact_id FROM repository_backups WHERE version=2').fetchone()[0]
        self.assertEqual(second['status'],'queued')
        self.assertIsNone(artifact)

    def test_partial_path_summary_is_bounded_on_replay_draft_and_reuse(self):
        prefix='共享目录'*7+'/'
        paths=[prefix+str(n) for n in range(12)]
        for name in paths:
            self.git('-C',str(self.source),'update-index','--add','--cacheinfo','160000,'+self.sha_a+','+name)
        self.git('-C',str(self.source),'commit','-qm','shared prefix')
        sha=self.git('-C',str(self.source),'rev-parse','HEAD').strip()
        self.enqueue_selected(sha)
        # Enough bytes for one escaped path but not two; separate count budget allows three.
        self.backups.SAMPLE_BYTES=330
        self.backups.SAMPLE_COUNT=3
        self.backups.run_once()
        receipt=self.result['receipt']
        def bounded(value):
            self.assertEqual(value['status'],'partial')
            self.assertLessEqual(len(json.dumps(value['missingAssets'])),self.backups.SAMPLE_BYTES)
            self.assertEqual(len(value['missingAssets']),1)
            self.assertEqual(value['missingAssetCount'],len(paths))
            self.assertEqual(value['missingAssetOmitted'],len(paths)-1)
        bounded(self.record())
        replay=self.formal_submit(self.selected)[1]
        self.assertEqual(replay['receipt'],receipt);bounded(replay['repositoryBackup'])
        status,draft,_=self.request(self.client,'/api/submission',{'requestId':'partial-draft-metadata','expectedVersion':1,'status':'draft','materials':receipt['materials']})
        self.assertEqual(status,200,draft);bounded(draft['repositoryBackup'])
        preview=self.sealed_preview()
        with patch.object(self.backups,'fetch_source',side_effect=AssertionError('reuse must not fetch')) as fetch:
            for version in (2,3):
                status,result,_=self.formal_submit(preview,version=version,request_id='partial-same-source-'+str(version))
                self.assertEqual(status,200,result);self.backups.run_once()
                bounded(self.request(self.client,'/api/submission/history/'+str(version+1))[1]['repositoryBackup'])
            fetch.assert_not_called()
        with self.service.db() as db:
            rows=db.execute('SELECT artifact_id,manifest_json FROM repository_backups').fetchall()
            self.assertEqual(len(rows),3)
            self.assertEqual(len({r['artifact_id'] for r in rows}),1)
            for r in rows:
                self.assertLess(len(r['manifest_json']),1500)
                self.assertEqual(json.loads(r['manifest_json'])['missingAssetCount'],len(paths))
            manifest=self.backups.artifact(rows[0]['artifact_id'],'manifest.json')
            self.assertCountEqual([x['path'] for x in json.loads(manifest.read_text())['missingAssets']],paths)
            source=self.repos.seals.source_for_version(db,self.team_id,1)
        # Three corrupt references to one physical artifact are checked once.
        from repository_backups import BackupFailure
        with patch.object(self.backups,'open_verified',side_effect=BackupFailure('artifact_unavailable')) as verify:
            self.assertEqual(self.backups.reusable(source),(None,None))
            self.assertEqual(verify.call_count,1)

    def test_queued_source_rejects_changed_organization_before_any_network(self):
        self.enqueue_selected(self.sha_a)
        provider=self.repos.providers['gitee']
        provider.organization='another-org'
        with patch.object(provider,'request',side_effect=AssertionError('must not request')) as request, patch.object(self.backups,'fetch_source') as fetch:
            self.backups.run_once()
            request.assert_not_called();fetch.assert_not_called()
        record=self.record()
        self.assertEqual((record['status'],record['error'],record['sha']),('queued','provider_unavailable',self.sha_a))

    def test_busy_completion_retries_published_result_without_refetch_or_attempt(self):
        from contextlib import contextmanager
        import sqlite3
        self.enqueue_selected(self.sha_a)
        receipt=self.result['receipt']
        original=self.service.db
        busy=[]
        @contextmanager
        def busy_once_after_publish():
            published=self.backups.root/'artifacts'
            if not busy and published.exists() and any(published.iterdir()):
                busy.append(True)
                raise sqlite3.OperationalError('database is locked')
            with original() as db:yield db
        with patch.object(self.service,'db',busy_once_after_publish),patch.object(self.backups,'fetch_source',wraps=self.backups.fetch_source) as fetch:
            for _ in range(2):
                try:self.backups.run_once()
                except sqlite3.OperationalError:pass  # same polling loop survives a busy finish
            self.assertEqual(busy,[True]);fetch.assert_called_once()
        self.assertEqual((self.record()['status'],self.record()['attempts']),('ready',1))
        self.assertEqual(len(list((self.backups.root/'artifacts').iterdir())),1)
        self.assertEqual(self.formal_submit(self.selected)[1]['receipt'],receipt)

    def test_prior_full_manifest_is_compacted_once_without_losing_private_download(self):
        self.enqueue_selected(self.sha_a);self.backups.run_once()
        with self.service.db() as db:
            row=db.execute('SELECT * FROM repository_backups').fetchone()
            old=json.loads(row['manifest_json'])
            paths=[{'path':'x'*900+str(i),'kind':'submodule'} for i in range(20)]
            old.pop('missingAssetCount',None);old.pop('missingAssetOmitted',None)
            old['missingAssets']=paths
            self.backups.artifact(row['artifact_id'],'manifest.json').write_text(json.dumps(old))
            db.execute("UPDATE repository_backups SET status='partial',manifest_json=?",(json.dumps(old),))
            db.execute("DELETE FROM submission_records_meta WHERE key='repositoryBackupSummaryV1'")
        with self.service.db() as db:self.backups.initialize(db)
        result=self.record()
        self.assertEqual((result['status'],result['missingAssetCount'],result['missingAssetOmitted']),('partial',20,20))
        self.assertEqual(result['missingAssets'],[])
        self.assertEqual(self.read_zip_file(result,'version.txt'),'A')
        with self.service.db() as db:
            self.assertLess(db.execute('SELECT length(manifest_json) FROM repository_backups').fetchone()[0],1500)
            self.assertEqual(json.loads(self.backups.artifact(row['artifact_id'],'manifest.json').read_text())['missingAssets'],paths)

    def test_sample_count_cap_and_empty_sample_preserve_partial_status(self):
        for path in ('module-a','module-b','module-c'):
            self.git('-C',str(self.source),'update-index','--add','--cacheinfo','160000,'+self.sha_a+','+path)
        self.git('-C',str(self.source),'commit','-qm','three gitlinks')
        self.enqueue_selected(self.git('-C',str(self.source),'rev-parse','HEAD').strip())
        with patch.object(self.backups,'SAMPLE_COUNT',1):self.backups.run_once()
        first=self.record()
        self.assertEqual((first['status'],len(first['missingAssets']),first['missingAssetOmitted']),('partial',1,2))
        self.git('-C',str(self.source),'commit','--allow-empty','-qm','same tree, new SHA')
        self.remote.sha=self.git('-C',str(self.source),'rev-parse','HEAD').strip()
        status,second,_=self.formal_submit(self.sealed_preview(),version=1,request_id='empty-sample-version')
        self.assertEqual(status,200,second)
        with patch.object(self.backups,'SAMPLE_BYTES',2):self.backups.run_once()
        value=self.request(self.client,'/api/submission/history/2')[1]['repositoryBackup']
        self.assertEqual((value['status'],value['missingAssets'],value['missingAssetCount'],value['missingAssetOmitted']),('partial',[],3,3))
