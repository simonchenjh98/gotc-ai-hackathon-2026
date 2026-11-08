"""Back up or restore an event database, its materials and private roadshow key."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


def _is_link(path):
    if path.is_symlink():return True
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def _material_stream(path, size, digest, output=None):
    """Verify a regular immutable file while copying it in bounded chunks."""
    if _is_link(path):raise ValueError('Material file must not be a symlink')
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size!=size:
            raise ValueError('Material size does not match the database')
        checksum=hashlib.sha256();copied=0
        while block:=stream.read(65536):
            copied+=len(block)
            if copied>size:raise ValueError('Material size changed while copying')
            checksum.update(block)
            if output is not None:output.write(block)
        if copied!=size or checksum.hexdigest()!=digest:
            raise ValueError('Material content does not match the database')


def copy_material_uploads(database, source_directory, destination_directory):
    """Copy exactly the completed material rows in this database snapshot."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_uploads'").fetchone():
        return {'count':0,'bytes':0}
    rows=list(database.execute('SELECT id,size,sha256 FROM material_uploads ORDER BY id'))
    if not rows:return {'count':0,'bytes':0}
    for file_id,size,digest in rows:
        if (not isinstance(file_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',file_id)
                or type(size) is not int or size<=0 or not isinstance(digest,str)
                or not re.fullmatch(r'[a-f0-9]{64}',digest)):
            raise ValueError('Invalid material metadata in database')
    source=Path(source_directory)/'material-uploads'
    destination=Path(destination_directory)/'material-uploads'
    if _is_link(source) or not source.is_dir():
        raise ValueError('Source material directory is missing or is a symlink')
    if _is_link(destination) or (destination.exists() and not destination.is_dir()):
        raise ValueError('Destination material directory must be a real directory')
    destination.mkdir(exist_ok=True,mode=0o700)
    created=[]
    try:
        for file_id,size,digest in rows:
            target=destination/file_id
            if _is_link(target):raise ValueError('Destination material must not be a symlink')
            try:
                descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            except FileExistsError:
                # Never replace existing bytes, even when the target filename matches.
                try:_material_stream(target,size,digest)
                except (OSError,ValueError):
                    raise ValueError('Refusing to overwrite different destination material') from None
                _material_stream(source/file_id,size,digest)
            else:
                created.append(target)
                with os.fdopen(descriptor,'wb') as output:
                    _material_stream(source/file_id,size,digest,output)
                    output.flush();os.fsync(output.fileno())
        return {'count':len(rows),'bytes':sum(row[1] for row in rows)}
    except Exception:
        for path in created:path.unlink(missing_ok=True)
        raise


def copy_roadshow_claim_key(database, source_directory, destination_directory):
    """Copy the immutable private key only after validating the backed-up database."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = 'roadshow_access_roster' in tables and database.execute('SELECT 1 FROM roadshow_access_roster LIMIT 1').fetchone() is not None
    binding = database.execute('SELECT key_digest FROM roadshow_access_config WHERE id=1').fetchone() if 'roadshow_access_config' in tables else None
    source = Path(source_directory) / 'roadshow-claim-key'
    destination = Path(destination_directory) / 'roadshow-claim-key'
    if source.is_symlink():
        raise ValueError('Roadshow claim key must not be a symlink')
    if not source.exists():
        if required:
            raise ValueError('The original roadshow claim key is required for this database')
        return False
    key = source.read_bytes()
    if len(key) != 32 or (binding and hashlib.sha256(key).hexdigest() != binding[0]) or (required and not binding):
        raise ValueError('Roadshow claim key does not match the database')
    if destination.is_symlink():
        raise ValueError('Destination roadshow claim key must not be a symlink')
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not destination.is_file() or destination.read_bytes() != key:
            raise ValueError('Refusing to overwrite a different destination roadshow claim key') from None
        os.chmod(destination, 0o600)
    else:
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return True


def copy_database(source, destination):
    source=Path(source).resolve();destination=Path(destination).absolute()
    if not source.is_file():raise ValueError('Source database does not exist')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    reader=None;writer=None
    try:
        reader=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        if reader.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Source integrity check failed')
        writer=sqlite3.connect(destination)
        reader.backup(writer)
        if writer.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or writer.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Copied database validation failed')
        tables=[row[0] for row in writer.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        rows={table:writer.execute('SELECT COUNT(*) FROM "'+table.replace('"','""')+'"').fetchone()[0] for table in tables}
        copied_key=copy_roadshow_claim_key(writer,source.parent,destination.parent)
        materials=copy_material_uploads(writer,source.parent,destination.parent)
        return {'source':str(source),'destination':str(destination),'integrity':'ok','tables':len(tables),'rows':rows,
                'privateFiles':['roadshow-claim-key'] if copied_key else [],'materialFiles':materials}
    except Exception:
        if writer:writer.close();writer=None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if writer:writer.close()
        if reader:reader.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('backup','restore'))
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path,help='New file only; restore never replaces an active database')
    args=parser.parse_args()
    try:result=copy_database(args.source,args.destination)
    except (OSError,ValueError,sqlite3.Error) as error:parser.exit(1,str(error)+'\n')
    print(json.dumps({'operation':args.operation,**result},ensure_ascii=False))


if __name__=='__main__':main()
