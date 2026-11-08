"""Immutable, bounded contestant files; HTML is always served in an opaque sandbox."""
import hashlib
import ipaddress
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import socket
import struct
import threading
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit
import zipfile

from errors import APIError


LIMITS = {'slides': 50 * 1024 * 1024, 'files': 100 * 1024 * 1024, 'demo': 10 * 1024 * 1024}
EXTENSIONS = {'slides': {'.ppt', '.pptx', '.pdf'}, 'files': {'.zip'}, 'demo': {'.html', '.htm'}}
MIMES = {'.ppt': 'application/vnd.ms-powerpoint', '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
         '.pdf': 'application/pdf', '.zip': 'application/zip', '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8'}
HTML_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' https:; "
            "style-src 'unsafe-inline' https:; img-src https: data: blob:; font-src https: data:; "
            "media-src https: data: blob:; connect-src https:; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SCHEMA = '''CREATE TABLE IF NOT EXISTS material_uploads (
 id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES teams(id),kind TEXT NOT NULL CHECK(kind IN ('slides','files','demo')),
 project_name TEXT NOT NULL,filename TEXT NOT NULL,size INTEGER NOT NULL CHECK(size>0),sha256 TEXT NOT NULL,created_at INTEGER NOT NULL);
 CREATE INDEX IF NOT EXISTS material_uploads_team ON material_uploads(team_id);
 CREATE TRIGGER IF NOT EXISTS material_uploads_no_update BEFORE UPDATE ON material_uploads BEGIN
 SELECT RAISE(ABORT,'uploaded materials are immutable'); END;
 CREATE TRIGGER IF NOT EXISTS material_uploads_no_delete BEFORE DELETE ON material_uploads BEGIN
 SELECT RAISE(ABORT,'uploaded materials are immutable'); END;'''


def header_text(headers, name, allow_empty=False):
    values = headers.get_all(name, [])
    if len(values) != 1 or len(values[0]) > 2048 or re.search(r'%(?![0-9a-fA-F]{2})', values[0]):
        raise APIError(400, '请填写作品名称并选择上传文件', 'upload_metadata')
    try:
        value = unquote(values[0], encoding='utf-8', errors='strict')
        value.encode('utf-8')
    except UnicodeError:
        raise APIError(400, '上传名称编码不正确', 'upload_metadata') from None
    if (not value.strip() and not allow_empty) or any(unicodedata.category(c).startswith('C') for c in value):
        raise APIError(400, '上传名称包含无效字符', 'upload_metadata')
    return value.strip()


def safe_part(value):
    value = ''.join('_' if c in '/\\:*?"<>|+%' or unicodedata.category(c).startswith('C') else c for c in value)
    # Bound the percent-encoded URL as well as UTF-8 filesystem/header lengths.
    value = value.strip(' .').encode('utf-8')[:70].decode('utf-8', errors='ignore').rstrip(' .')
    return value or '作品'


def origin_key(url):
    host = url.hostname.rstrip('.').encode('idna').decode('ascii').lower()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            # Parse legacy browser IPv4 spellings locally, without DNS or network I/O.
            host = str(ipaddress.IPv4Address(socket.inet_aton(host)))
        except OSError:
            pass
    return host, url.port or (443 if url.scheme == 'https' else 80)


class MaterialUploads:
    TEAM_QUOTA = 500 * 1024 * 1024
    TEAM_COUNT = 250
    MIN_FREE_BYTES = 1024 * 1024 * 1024

    def __init__(self, service):
        self.s = service
        self.root = service.data_dir / 'material-uploads'
        if self.root.is_symlink():
            raise ValueError('Material storage must not be a symbolic link')
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock = threading.Lock()
        self.active_teams = set()
        self.slots = threading.BoundedSemaphore(4)

    def initialize(self, db):
        db.executescript(SCHEMA)

    @staticmethod
    def kind(path):
        match = re.fullmatch(r'/api/team/uploads/(slides|files|demo)', path)
        return match.group(1) if match else None

    def identity(self, db, cookie, login_id):
        actor = self.s.account(db, cookie)
        self.s.require(actor, 'team')
        if not login_id:
            raise APIError(428, '请刷新页面并核对当前队伍', 'login_required')
        if login_id != self.s.login_id(cookie):
            raise APIError(401, '登录身份已变化，请重新登录后上传')
        team = self.s.bound_submission_team(db, actor)
        if not self.s.submission_window(db)['canEdit']:
            raise APIError(409, '作品提交尚未开放或已截止，不能上传文件', 'submission_closed')
        return actor, team

    def quota(self, db, team_id, length):
        size, count = db.execute('SELECT COALESCE(SUM(size),0),COUNT(*) FROM material_uploads WHERE team_id=?', (team_id,)).fetchone()
        if size + length > self.TEAM_QUOTA or count >= self.TEAM_COUNT:
            raise APIError(413, '本队上传总量已达上限，请联系组织方（每队500 MiB）', 'upload_quota')

    def url(self, row):
        return self.s.origin + self.s.base_path + '/materials/' + row['id'] + '/' + quote(row['filename'], safe='')

    def upload(self, handler, kind, length):
        if length <= 0 or length > LIMITS[kind]:
            raise APIError(413, '文件超过大小限制或为空', 'upload_size')
        project = header_text(handler.headers, 'X-GOTC-Project', allow_empty=True) or '作品'
        original = header_text(handler.headers, 'X-GOTC-Filename')
        if len(project) > 140:
            raise APIError(400, '作品名称最多140字', 'upload_metadata')
        extension = Path(original).suffix.lower()
        if extension not in EXTENSIONS[kind]:
            raise APIError(415, '该提交项不支持此文件类型', 'upload_type')
        cookie, login_id = handler.headers.get('Cookie', ''), handler.headers.get('X-GOTC-Login')
        with self.s.db() as db:
            _, team = self.identity(db, cookie, login_id)
        team_id = team['id']
        if not self.slots.acquire(blocking=False):
            raise APIError(429, '正在处理其他上传，请稍后重试', 'upload_busy')
        acquired = False
        temporary = target = None
        committed = False
        try:
            with self.lock:
                if team_id in self.active_teams:
                    raise APIError(429, '本队已有文件正在上传，请等待完成', 'upload_busy')
                self.active_teams.add(team_id)
                acquired = True
            with self.s.db() as db:
                self.quota(db, team_id, length)
            if shutil.disk_usage(self.root).free - length < self.MIN_FREE_BYTES:
                raise APIError(507, '服务器存储空间不足，请保留本地文件并联系组织方', 'storage_full')
            identifier = secrets.token_urlsafe(32)
            temporary, target = self.root / ('.pending-' + identifier), self.root / identifier
            digest = hashlib.sha256()
            # Never keep a database transaction open while reading contestant bytes.
            with temporary.open('xb') as stream:
                os.chmod(temporary, 0o600)
                remaining = length
                started = time.monotonic()
                while remaining:
                    if time.monotonic() - started > 300:
                        raise APIError(408, '上传超时，请重新上传', 'upload_timeout')
                    chunk = handler.rfile.read1(min(65536, remaining))
                    if not chunk:
                        raise APIError(400, '文件上传不完整，请重试', 'upload_incomplete')
                    stream.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            self.validate_type(temporary, extension)
            with self.s.db() as db:
                db.execute('BEGIN IMMEDIATE')
                actor, current_team = self.identity(db, cookie, login_id)
                if current_team['id'] != team_id:
                    raise APIError(401, '当前队伍已变化，请重新上传')
                self.quota(db, team_id, length)
                filename = safe_part(current_team['name']) + '+' + safe_part(project) + extension
                row = {'id': identifier, 'team_id': team_id, 'kind': kind, 'project_name': project,
                       'filename': filename, 'size': length, 'sha256': digest.hexdigest(), 'created_at': int(time.time() * 1000)}
                os.replace(temporary, target)
                if os.name != 'nt':
                    descriptor = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                db.execute('INSERT INTO material_uploads VALUES (:id,:team_id,:kind,:project_name,:filename,:size,:sha256,:created_at)', row)
                self.s.audit(db, actor, 'material.upload', team_id, {'id': identifier, 'kind': kind, 'size': length})
            committed = True
            return {'url': self.url(row), 'filename': filename, 'size': length, 'kind': kind}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if not committed and target is not None:
                target.unlink(missing_ok=True)
            if acquired:
                with self.lock:
                    self.active_teams.discard(team_id)
            self.slots.release()

    @staticmethod
    def validate_type(path, extension):
        with path.open('rb') as stream:
            head = stream.read(512)
        valid = False
        if extension == '.pdf':
            valid = head.startswith(b'%PDF-')
        elif extension == '.ppt':
            valid = head.startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
        elif extension in {'.zip', '.pptx'} and head.startswith((b'PK\x03\x04', b'PK\x05\x06')):
            # Bound central-directory allocation; archives are never extracted.
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 65577))
                    tail = stream.read()
                index = tail.rfind(b'PK\x05\x06')
                end = struct.unpack('<4s4H2LH', tail[index:index + 22]) if index >= 0 else None
                zip64 = index >= 20 and tail[index - 20:index - 16] == b'PK\x06\x07'
                if end and not zip64 and end[1] == end[2] == 0 and end[3] == end[4] and end[4] <= 10000 and end[5] <= 4 * 1024 * 1024 and index + 22 + end[7] == len(tail):
                    with zipfile.ZipFile(path) as archive:
                        names = set(archive.namelist())
                        valid = extension == '.zip' or {'[Content_Types].xml', 'ppt/presentation.xml'}.issubset(names)
            except (OSError, ValueError, struct.error, zipfile.BadZipFile, NotImplementedError):
                valid = False
        elif extension in {'.html', '.htm'}:
            try:
                content = path.read_text(encoding='utf-8-sig')
                valid = '\x00' not in content and bool(re.search(r'<(?:!doctype\s+html|html|head|body|title|script|style|div|main|section|h[1-6]|p)\b', content, re.I))
            except UnicodeError:
                valid = False
        if not valid:
            raise APIError(415, '文件内容与格式不符；HTML请使用UTF-8编码，PPTX和ZIP请使用普通ZIP格式', 'upload_type')

    def validate_materials(self, db, team_id, materials):
        own = urlsplit(self.s.origin)
        prefix = self.s.base_path + '/materials/'
        team_name = db.execute('SELECT name FROM teams WHERE id=?', (team_id,)).fetchone()['name']
        for field, value in materials.items():
            if not field.endswith('Url') or not value:
                continue
            parsed = urlsplit(value)
            if origin_key(parsed) != origin_key(own):
                continue
            path = posixpath.normpath(unquote(parsed.path))
            if not path.startswith(prefix):
                continue
            match = re.fullmatch(re.escape(prefix) + r'([A-Za-z0-9_-]{43})/([^/]+)', path)
            row = db.execute('SELECT * FROM material_uploads WHERE id=?', (match[1],)).fetchone() if match else None
            if not row or value != self.url(row) or not (self.root / row['id']).is_file():
                raise APIError(400, '上传文件不存在或链接不完整，请重新上传', 'upload_reference')
            if row['team_id'] != team_id:
                raise APIError(403, '只能提交本队上传的文件', 'upload_owner')
            if {'slidesUrl': 'slides', 'filesUrl': 'files', 'demoUrl': 'demo'}.get(field) != row['kind']:
                raise APIError(400, '上传文件与提交项不匹配，请选择对应文件', 'upload_kind')
            project_name = materials['projectName'] or row['project_name']
            expected_name = safe_part(team_name) + '+' + safe_part(project_name) + Path(row['filename']).suffix
            if row['project_name'] != project_name or row['filename'] != expected_name:
                raise APIError(400, '队名或作品名称已变化，请按新名称重新上传文件', 'upload_project')

    def serve(self, handler, path):
        match = re.fullmatch(r'/materials/([A-Za-z0-9_-]{43})/([^/]+)', path)
        with self.s.db() as db:
            row = db.execute('SELECT * FROM material_uploads WHERE id=?', (match[1],)).fetchone() if match else None
        if not row or match[2] != row['filename']:
            raise APIError(404, '文件不存在')
        source = self.root / row['id']
        if source.is_symlink() or not source.is_file() or source.stat().st_size != row['size']:
            raise APIError(404, '文件暂不可用，请联系组织方')
        extension = Path(row['filename']).suffix.lower()
        html = row['kind'] == 'demo'
        disposition = 'inline' if html else 'attachment'
        extra = {'Content-Disposition': disposition + "; filename=\"gotc-material" + extension + "\"; filename*=UTF-8''" + quote(row['filename'], safe=''),
                 'Referrer-Policy': 'no-referrer'}
        if html:
            extra.update({'Content-Security-Policy': HTML_CSP, 'Cross-Origin-Opener-Policy': 'same-origin'})
        with source.open('rb') as stream:
            handler.headers_out(200, MIMES[extension], row['size'], extra=extra)
            if handler.command != 'HEAD':
                while chunk := stream.read(65536):
                    handler.wfile.write(chunk)
