"""Back up or restore an event database and its private roadshow key without overwrites."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3


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
        return {'source':str(source),'destination':str(destination),'integrity':'ok','tables':len(tables),'rows':rows,
                'privateFiles':['roadshow-claim-key'] if copied_key else []}
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
