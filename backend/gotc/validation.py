"""Validation and normalization shared by the event domains."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .errors import APIError


def validate_unicode(value):
    """Reject unpaired surrogate code points before hashing, SQLite or UTF-8 output.

    json.loads already combines valid escaped surrogate pairs into real Unicode
    characters. Walk iteratively so nested JSON values and object keys are covered.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeEncodeError:
                raise APIError(400, '文本包含无效的 Unicode 字符，请重新输入', 'invalid_unicode') from None
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def text_field(value, field, limit, required=False):
    if not isinstance(value, str):
        raise APIError(400, f"{field}必须是文字")
    validate_unicode(value)
    value = value.strip()
    if len(value) > limit or (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise APIError(400, f"{field}长度或内容不正确")
    return value


def valid_material_host(host):
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", host):
        return False
    try:
        domain=host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return len(domain)<=253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.I) for label in domain.split("."))


COMMIT_LINK_HOSTS={'github.com','gitee.com','gitcode.com','gitlab.com'}


def repository_parts(value, commit=False):
    """Parse supported public-host URL shapes only; never contact the host."""
    try:
        url=urlsplit(value)
        host=url.hostname
        if (url.scheme!='https' or host not in COMMIT_LINK_HOSTS or url.username is not None
                or url.password is not None or url.netloc.lower()!=host or url.port is not None or url.query or url.fragment
                or re.search(r'[\s\\%]',value) or len(value.encode('utf-8'))>1024):
            return None
        parts=url.path.rstrip('/').split('/')[1:]
        sha=None
        if commit:
            marker=['-','commit'] if host=='gitlab.com' else ['commit']
            if host=='gitcode.com' and parts[-3:-1]==['commits','detail']:
                marker=['commits','detail']
            tail=len(marker)+1
            if len(parts)<tail+2 or parts[-tail:-1]!=marker:
                return None
            sha=parts[-1]
            if not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',sha):
                return None
            parts=parts[:-tail]
        if not parts or not parts[-1]:
            return None
        parts[-1]=re.sub(r'\.git$','',parts[-1])
        # GitHub repository names may start with a dot; owners and other hosts keep their existing grammar.
        if len(parts)<2 or (host!='gitlab.com' and len(parts)!=2) or any(not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*' if host=='github.com' and index==len(parts)-1 else r'[A-Za-z0-9_][A-Za-z0-9_.-]*',part) or part in {'.','..'} for index,part in enumerate(parts)):
            return None
        # Fold only hosts whose existing contract compares names case-insensitively.
        path='/'.join(parts)
        return host,path.lower() if host in {'github.com','gitee.com'} else path,sha.lower() if sha else None
    except (ValueError,TypeError):
        return None


def normalize_code_version(repository, version, allow_empty=False):
    try:
        url=urlsplit(repository)
        valid=(url.scheme=='https' and valid_material_host(url.hostname) and url.username is None and url.password is None
               and url.port!=0 and len(repository.encode('utf-8'))<=1024
               and not re.search(r'[\s\\\x00-\x1f\x7f]',repository)
               and not re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)',repository,re.I))
    except (ValueError,TypeError):
        valid=False
    repo=repository_parts(repository)
    if not valid or (url.hostname in COMMIT_LINK_HOSTS and not repo):
        raise APIError(400,'仓库请填写完整 HTTPS 仓库主页链接，不是本地路径或文件页面；GitHub、Gitee、GitCode、GitLab 请使用仓库根地址')
    if allow_empty and not version:
        return ''
    if re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',version):
        return version.lower()
    commit=repository_parts(version,commit=True)
    if not repo or not commit or commit[:2]!=repo[:2]:
        raise APIError(400,'commit 请填写同一 GitHub、Gitee、GitCode 或 GitLab 仓库的完整提交页面链接，或40/64位完整哈希；其他平台请填完整哈希，不要填写分支、tag或前6位')
    return commit[2]


def csv_safe(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value
