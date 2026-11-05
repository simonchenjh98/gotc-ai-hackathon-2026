"""Private bounded Git backups. No checkout, project execution, or public file mapping."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from urllib.parse import quote

from errors import APIError

SCHEMA='''CREATE TABLE IF NOT EXISTS repository_backups (
 team_id TEXT NOT NULL,version INTEGER NOT NULL,request_id TEXT NOT NULL,
 status TEXT NOT NULL,sha TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0,error TEXT,artifact_id TEXT,manifest_json TEXT,
 PRIMARY KEY(team_id,version),UNIQUE(team_id,request_id),
 FOREIGN KEY(team_id,version) REFERENCES repository_submission_seals(team_id,version));'''


class BackupFailure(Exception):
    """Only fixed, non-secret error codes may escape the worker."""


@dataclass
class ArchiveDownload:
    stream: object
    size: int
    filename: str
    content_type: str


class RepositoryBackups:
    POLL_SECONDS=5
    MAX_ATTEMPTS=3
    RETRY_SECONDS=(60,300)
    GIT_SECONDS=180
    TASK_BYTES=268435456
    TOTAL_BYTES=2147483648
    SOURCE_BYTES=134217728
    FILE_COUNT=20000

    def __init__(self,service):
        self.s=service
        self.root=service.data_dir/'repository-backups'
        self.clock=time.time
        self.stopping=threading.Event()
        self.lock=threading.Lock()
        self.process_lock=threading.Lock()
        self.process=None
        self.thread=None
        self.username=None
        self.git_binary=shutil.which('git')

    def directory(self,path):
        # Reject any symlink component; never resolve untrusted artifacts into another root.
        relative=path.relative_to(self.s.data_dir)
        current=self.s.data_dir
        for part in relative.parts:
            current=current/part
            if current.is_symlink():raise BackupFailure('artifact_unavailable')
            current.mkdir(mode=0o700,exist_ok=True)
            if not current.is_dir():raise BackupFailure('artifact_unavailable')
            current.chmod(0o700)
        return path

    def initialize(self,db):
        db.executescript(SCHEMA)
        db.execute("UPDATE repository_backups SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,error='worker_interrupted' WHERE status='running'",(self.MAX_ATTEMPTS,))

    def enqueue(self,db,team_id,version,request_id):
        seal=self.s.repositories.seals.source_for_version(db,team_id,version)
        if seal:
            db.execute("INSERT OR IGNORE INTO repository_backups(team_id,version,request_id,status,sha) VALUES (?,?,?,'queued',?)",(team_id,version,request_id,seal['sha']))
        return self.for_version(db,team_id,version)

    def artifact(self,artifact_id,name):
        if not isinstance(artifact_id,str) or not re.fullmatch('[0-9a-f]{48}',artifact_id):raise BackupFailure('artifact_unavailable')
        path=self.root/'artifacts'/artifact_id/name
        for parent in (self.root,self.root/'artifacts',path.parent,path):
            if parent.is_symlink():raise BackupFailure('artifact_unavailable')
        return path

    def open_verified(self,row,name):
        manifest=json.loads(row['manifest_json'])
        item=manifest['files'][name]
        path=self.artifact(row['artifact_id'],name)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        stream=os.fdopen(fd,'rb')
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=item['bytes'] or info.st_size>self.TASK_BYTES:raise BackupFailure('artifact_unavailable')
            digest=hashlib.sha256()
            while chunk:=stream.read(65536):digest.update(chunk)
            if digest.hexdigest()!=item['sha256']:raise BackupFailure('artifact_unavailable')
            stream.seek(0)
            return stream,info.st_size
        except BaseException:
            stream.close()
            raise

    def for_version(self,db,team_id,version):
        row=db.execute('SELECT * FROM repository_backups WHERE team_id=? AND version=?',(team_id,version)).fetchone()
        if not row:return None
        result=dict(status=row['status'],sha=row['sha'],attempts=row['attempts'],error=row['error'],sourceZipSha256=None,bundleSha256=None,missingAssets=[],downloadUrl=None,bundleUrl=None)
        if row['status'] in {'ready','partial'}:
            try:
                manifest=json.loads(row['manifest_json'])
                # Publication already verified these hashes. Responses may hold the
                # submission writer, so only bounded filesystem metadata checks belong here.
                if manifest['sha']!=row['sha']:raise BackupFailure('artifact_unavailable')
                for name in ('source.zip','repo.bundle'):
                    item=manifest['files'][name]
                    info=self.artifact(row['artifact_id'],name).lstat()
                    if (not stat.S_ISREG(info.st_mode) or type(item['bytes']) is not int
                            or not 0<=info.st_size==item['bytes']<=self.TASK_BYTES
                            or not isinstance(item['sha256'],str)
                            or not re.fullmatch('[0-9a-f]{64}',item['sha256'])):
                        raise BackupFailure('artifact_unavailable')
                result.update(sourceZipSha256=manifest['files']['source.zip']['sha256'],bundleSha256=manifest['files']['repo.bundle']['sha256'],missingAssets=manifest['missingAssets'])
                base=self.s.base_path+'/api/submission/backups/'+quote(team_id,safe='')+'/'+str(version)
                result.update(downloadUrl=base+'/source.zip',bundleUrl=base+'/repo.bundle')
            except (OSError,ValueError,KeyError,BackupFailure):
                result.update(status='failed',error='artifact_unavailable')
        return result

    def download(self,db,actor,method,path,query):
        if not actor:raise APIError(401,'请先登录')
        match=re.fullmatch(r'/api/submission/backups/([^/]+)/([1-9][0-9]{0,15})/(source\.zip|repo\.bundle)',path)
        if not match or method!='GET' or query:raise APIError(404,'备份不存在')
        team_id,version,name=match.groups()
        if actor['role']!='admin':
            self.s.require(actor,'team')
            if self.s.bound_submission_team(db,actor)['id']!=team_id:raise APIError(403,'无权下载该队备份')
        row=db.execute("SELECT * FROM repository_backups WHERE team_id=? AND version=? AND status IN ('ready','partial')",(team_id,int(version))).fetchone()
        if not row:raise APIError(404,'备份尚未就绪')
        try:stream,size=self.open_verified(row,name)
        except (OSError,ValueError,KeyError,BackupFailure):raise APIError(404,'备份文件不可用') from None
        return ArchiveDownload(stream,size,name,'application/zip' if name=='source.zip' else 'application/octet-stream')

    def usage(self,path):
        total=0
        if path.exists():
            for root,dirs,files in os.walk(path,followlinks=False):
                for name in dirs+files:
                    entry=Path(root)/name
                    if entry.is_symlink():raise BackupFailure('unsafe_path')
                for name in files:
                    try:total+=(Path(root)/name).stat().st_size
                    except FileNotFoundError:pass  # Git atomically renames pack/lock files while polled.
        return total

    def check(self,ctx):
        if self.stopping.is_set():raise BackupFailure('worker_interrupted')
        if time.monotonic()>=ctx['deadline']:raise BackupFailure('git_timeout')
        if self.usage(ctx['scratch'])>self.TASK_BYTES:raise BackupFailure('task_limit')
        if self.usage(self.root)>self.TOTAL_BYTES:raise BackupFailure('storage_limit')
        if shutil.disk_usage(self.root).free<1048576:raise BackupFailure('storage_limit')

    def _git(self,repo,args,ctx,env=None,input_path=None):
        if not self.git_binary:raise BackupFailure('git_unavailable')
        self.check(ctx)
        output=ctx['scratch']/('output-'+secrets.token_hex(8))
        # Finite per-file RLIMIT_FSIZE additionally bounds pack/output writes between polling.
        remaining=min(self.TASK_BYTES-self.usage(ctx['scratch']),self.TOTAL_BYTES-self.usage(self.root))
        if remaining<4096:
            raise BackupFailure('task_limit' if self.TASK_BYTES-self.usage(ctx['scratch'])<=self.TOTAL_BYTES-self.usage(self.root) else 'storage_limit')
        argv=[self.git_binary,'-c','credential.helper=','-c','core.hooksPath=/dev/null','-c','http.followRedirects=false','-c','protocol.allow=never','-c','protocol.https.allow=always','-c','pack.threads=1','-c','pack.windowMemory=32m','-c','gc.auto=0','-C',str(repo),*args]
        wrapper='import os,resource,sys; n=int(sys.argv[1]); resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); os.execve(sys.argv[2],sys.argv[2:],os.environ)'
        try:
            with output.open('xb') as stdout, (input_path.open('rb') if input_path else open(os.devnull,'rb')) as stdin:
                with self.process_lock:
                    if self.stopping.is_set():raise BackupFailure('worker_interrupted')
                    self.process=subprocess.Popen([sys.executable,'-c',wrapper,str(remaining),*argv],stdin=stdin,stdout=stdout,stderr=subprocess.DEVNULL,env=env or ctx['env'],start_new_session=True)
                    process=self.process
                try:
                    while process.poll() is None:
                        self.check(ctx)
                        time.sleep(.02)
                    self.check(ctx)
                    if process.returncode:
                        if self.usage(ctx['scratch'])>=self.TASK_BYTES:raise BackupFailure('task_limit')
                        if self.usage(self.root)>=self.TOTAL_BYTES:raise BackupFailure('storage_limit')
                        raise BackupFailure('git_failed')
                finally:
                    with self.process_lock:
                        if process.poll() is None:
                            try:os.killpg(process.pid,signal.SIGKILL)
                            except ProcessLookupError:pass
                        process.wait()
                        self.process=None
            return output
        except OSError:raise BackupFailure('git_unavailable') from None

    def fetch_source(self,repo,source,env,ctx):
        # Source identity is immutable local seal evidence; no user URL or branch input.
        provider=self.s.repositories.providers['gitee']
        if provider.name!='gitee' or provider.origin!='https://gitee.com/api/v5' or not provider.enabled:raise BackupFailure('provider_unavailable')
        if self.username is None:
            status,user=provider.request('GET','/user')
            login=user.get('login') if isinstance(user,dict) else None
            if status!=200 or not isinstance(login,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',login):raise BackupFailure('provider_identity')
            self.username=login
        askpass=ctx['scratch']/'askpass'
        # This helper contains no credential; environment exists only for authenticated fetch.
        askpass.write_text('#!'+sys.executable+'\nimport os,sys\nprint(os.environ["GOTC_GIT_USER"] if "Username" in sys.argv[1] else os.environ["GOTC_GIT_TOKEN"])\n')
        askpass.chmod(0o700)
        fetch_env={**env,'GIT_ASKPASS':str(askpass),'GOTC_GIT_USER':self.username,'GOTC_GIT_TOKEN':provider.token}
        remote='https://gitee.com/'+source['organization']+'/'+source['slug']+'.git'
        try:self._git(repo,['fetch','--no-tags','--no-recurse-submodules',remote,'+refs/heads/'+source['ref']+':refs/heads/fetched'],ctx,fetch_env)
        finally:askpass.unlink(missing_ok=True)

    def tree_records(self,path,ctx):
        pending=b''
        with path.open('rb') as stream:
            while chunk:=stream.read(65536):
                self.check(ctx)
                records=(pending+chunk).split(b'\0')
                pending=records.pop()
                if len(pending)>8192:raise BackupFailure('unsafe_path')
                yield from records
            if pending:raise BackupFailure('object_invalid')

    def build(self,source,scratch):
        if not all(re.fullmatch(r'[A-Za-z0-9_-]+',source[k]) for k in ('organization','slug')) or not re.fullmatch(r'gotc-archive-[0-9a-f]{32}-[0-9a-f]{40,64}',source['ref']) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',source['sha']):raise BackupFailure('source_identity')
        env={'PATH':'/usr/bin:/bin','HOME':str(scratch),'LANG':'C','LC_ALL':'C','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_TERMINAL_PROMPT':'0','GIT_ATTR_NOSYSTEM':'1','GIT_LFS_SKIP_SMUDGE':'1'}
        ctx={'scratch':scratch,'deadline':time.monotonic()+self.GIT_SECONDS,'env':env}
        repo=self.directory(scratch/'repo')
        self._git(repo,['init','--template=','--bare','-q','.'],ctx)
        provider=self.s.repositories.providers['gitee']
        self.s.repositories.verify_repository(source,provider.repository(source))
        self.fetch_source(repo,source,env,ctx)
        sha=source['sha']
        exists=self._git(repo,['rev-parse','--verify',sha+'^{commit}'],ctx).read_text().strip()
        if exists!=sha:raise BackupFailure('selected_commit_missing')
        self._git(repo,['merge-base','--is-ancestor',sha,'refs/heads/fetched'],ctx)
        self._git(repo,['update-ref','refs/heads/selected',sha],ctx)
        publish=self.directory(scratch/'publish')
        bundle=publish/'repo.bundle'
        self._git(repo,['symbolic-ref','HEAD','refs/heads/selected'],ctx)
        self._git(repo,['bundle','create',str(bundle),'refs/heads/selected','HEAD'],ctx)
        empty=self.directory(scratch/'verify')
        self._git(empty,['init','--template=','--bare','-q','.'],ctx)
        self._git(empty,['bundle','verify',str(bundle)],ctx)
        tree=self._git(repo,['ls-tree','-r','-l','-z',sha],ctx)
        entries=[];missing=[];source_bytes=0
        for raw in self.tree_records(tree,ctx):
            header,raw_path=raw.split(b'\t',1)
            mode,kind,oid,size=header.split()
            path=raw_path.decode('utf-8')
            if len(raw_path)>4096 or path.startswith('/') or ':' in path or '\\' in path or any(ord(c)<32 for c in path) or any(p in {'.','..','.git',''} for p in path.split('/')) or PurePosixPath(path).is_absolute():raise BackupFailure('unsafe_path')
            if len(entries)+len(missing)>=self.FILE_COUNT:raise BackupFailure('file_count_limit')
            if mode==b'160000':missing.append({'path':path,'kind':'submodule'});continue
            if kind!=b'blob' or mode not in {b'100644',b'100755',b'120000'}:raise BackupFailure('unsafe_path')
            size=int(size);source_bytes+=size
            if source_bytes>self.SOURCE_BYTES:raise BackupFailure('source_limit')
            entries.append((path,mode,oid,size))
        request=scratch/'blob-request'
        request.write_bytes(b''.join(oid+b'\n' for _,_,oid,_ in entries))
        blobs=self._git(repo,['cat-file','--batch'],ctx,input_path=request)
        with blobs.open('rb') as content,zipfile.ZipFile(publish/'source.zip','w',zipfile.ZIP_DEFLATED,allowZip64=False) as archive:
            for path,mode,oid,size in entries:
                if content.readline().split()!=[oid,b'blob',str(size).encode()]:raise BackupFailure('object_invalid')
                info=zipfile.ZipInfo(path,date_time=(1980,1,1,0,0,0));info.create_system=3
                info.external_attr=int(mode,8)<<16;info.compress_type=zipfile.ZIP_DEFLATED
                prefix=b'';remaining=size
                with archive.open(info,'w') as target:
                    while remaining:
                        self.check(ctx)
                        chunk=content.read(min(65536,remaining))
                        if not chunk:raise BackupFailure('object_invalid')
                        prefix=(prefix+chunk)[:1024];target.write(chunk);remaining-=len(chunk)
                if mode==b'120000':
                    target=prefix.decode('utf-8')
                    if size>1024 or target.startswith('/') or '\\' in target or ':' in target or '..' in target.split('/'):
                        raise BackupFailure('unsafe_path')
                if content.read(1)!=b'\n':raise BackupFailure('object_invalid')
                if prefix.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                    missing.append({'path':path,'kind':'lfs'})
        manifest={'sha':sha,'repositoryId':source['external_id'],'organization':source['organization'],'slug':source['slug'],'ref':source['ref'],'sourceBytes':source_bytes,'fileCount':len(entries),'missingAssets':missing,'files':{}}
        for name in ('source.zip','repo.bundle'):
            path=publish/name;digest=hashlib.sha256()
            with path.open('rb') as stream:
                while chunk:=stream.read(65536):self.check(ctx);digest.update(chunk)
            path.chmod(0o600)
            manifest['files'][name]={'bytes':path.stat().st_size,'sha256':digest.hexdigest()}
        (publish/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=True,sort_keys=True))
        (publish/'manifest.json').chmod(0o600)
        self.check(ctx)
        return publish,manifest

    def reusable(self,source):
        with self.s.db() as db:
            candidates=db.execute("SELECT b.* FROM repository_backups b JOIN repository_submission_seals v ON v.team_id=b.team_id AND v.version=b.version JOIN repository_seals s ON s.id=v.seal_id JOIN repository_seal_namespaces n USING(prefix) WHERE b.status IN ('ready','partial') AND s.sha=? AND n.external_id=? AND n.organization=? AND n.slug=? AND s.prefix=?",(source['sha'],source['external_id'],source['organization'],source['slug'],source['prefix'])).fetchall()
        for row in candidates:
            try:
                manifest=json.loads(row['manifest_json'])
                if any(manifest[key]!=source[field] for key,field in (('sha','sha'),('repositoryId','external_id'),('organization','organization'),('slug','slug'),('ref','ref'))):continue
                for name in ('source.zip','repo.bundle'):
                    stream,_=self.open_verified(row,name)
                    stream.close()
                return row['artifact_id'],manifest
            except (OSError,ValueError,KeyError,BackupFailure):continue
        return None,None

    def run_once(self):
        if not self.lock.acquire(blocking=False):return False
        scratch=None
        try:
            if self.stopping.is_set():return False
            with self.s.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT * FROM repository_backups WHERE status='queued' AND next_at<=? AND attempts<? ORDER BY next_at,team_id,version LIMIT 1",(self.clock(),self.MAX_ATTEMPTS)).fetchone()
                if not row:return False
                job=dict(row);job['attempts']+=1
                db.execute("UPDATE repository_backups SET status='running',attempts=?,error=NULL WHERE team_id=? AND version=?",(job['attempts'],job['team_id'],job['version']))
                source=self.s.repositories.seals.source_for_version(db,job['team_id'],job['version'])
            artifact_id=None;manifest=None;error=None
            try:
                artifact_id,manifest=self.reusable(source)
                if manifest is None:
                    self.directory(self.root/'scratch');self.directory(self.root/'artifacts')
                    scratch=self.directory(self.root/'scratch'/secrets.token_hex(24))
                    publish,manifest=self.build(source,scratch)
                    artifact_id=secrets.token_hex(24)
                    os.replace(publish,self.root/'artifacts'/artifact_id)
                status='partial' if manifest['missingAssets'] else 'ready'
            except Exception as failure:
                allowed={'artifact_unavailable','worker_interrupted','git_timeout','task_limit','storage_limit','git_unavailable','git_failed','provider_unavailable','provider_identity','source_identity','selected_commit_missing','unsafe_path','file_count_limit','source_limit','object_invalid'}
                error=str(failure) if isinstance(failure,BackupFailure) and str(failure) in allowed else 'backup_failed'
                status='queued' if job['attempts']<self.MAX_ATTEMPTS else 'failed'
            finally:
                if scratch and not scratch.is_symlink():shutil.rmtree(scratch,ignore_errors=True)
            with self.s.db() as db:
                db.execute('BEGIN IMMEDIATE')
                delay=self.RETRY_SECONDS[min(job['attempts']-1,len(self.RETRY_SECONDS)-1)] if status=='queued' else 0
                db.execute('UPDATE repository_backups SET status=?,next_at=?,error=?,artifact_id=?,manifest_json=? WHERE team_id=? AND version=?',
                           (status,self.clock()+delay,error,artifact_id,json.dumps(manifest) if manifest else None,job['team_id'],job['version']))
            return True
        finally:self.lock.release()

    def start(self):
        if os.environ.get('GOTC_REPO_BACKUP_ENABLED')!='1' or self.thread and self.thread.is_alive():return
        self.stopping.clear()
        # Recover uncommitted private publications and interrupted scratch only at startup.
        self.directory(self.root/'scratch');self.directory(self.root/'artifacts')
        with self.s.db() as db:known={r[0] for r in db.execute('SELECT artifact_id FROM repository_backups WHERE artifact_id IS NOT NULL')}
        for parent in (self.root/'scratch',self.root/'artifacts'):
            for child in parent.iterdir():
                if parent.name=='scratch' or child.name not in known:
                    if child.is_symlink():child.unlink()
                    elif child.is_dir():shutil.rmtree(child)
        def loop():
            while not self.stopping.is_set():
                try:self.run_once()
                except Exception:pass  # No exception body can leak subprocess/provider credentials.
                self.stopping.wait(self.POLL_SECONDS)
        self.thread=threading.Thread(target=loop,name='repository-backups',daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.process_lock:
            if self.process and self.process.poll() is None:
                try:os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        if self.thread:self.thread.join(30)
