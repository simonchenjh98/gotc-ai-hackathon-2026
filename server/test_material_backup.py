"""Material files remain recoverable with their database snapshot; synthetic data only."""
from contextlib import closing
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]


def load_tool():
    spec=importlib.util.spec_from_file_location('material_backup_tool',ROOT/'scripts'/'backup_event.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MaterialBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='gotc-material-backup-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.source=self.root/'source'/'event.sqlite3'
        self.source.parent.mkdir()
        (self.source.parent/'material-uploads').mkdir()
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('CREATE TABLE material_uploads (id TEXT PRIMARY KEY,size INTEGER NOT NULL,sha256 TEXT NOT NULL)')
            db.commit()
        self.tool=load_tool()

    def material(self, letter='a', contents=b'Synthetic submitted material'):
        file_id=letter*43
        path=self.source.parent/'material-uploads'/file_id
        path.write_bytes(contents)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('INSERT INTO material_uploads VALUES (?,?,?)',(file_id,len(contents),hashlib.sha256(contents).hexdigest()))
            db.commit()
        return path

    def destination(self, name='backup'):
        return self.root/name/'event.sqlite3'

    def test_roundtrip_copies_completed_rows_and_excludes_pending_or_unrecorded_files(self):
        original=self.material()
        self.material('b',b'<html>Directly viewable synthetic demo</html>')
        (original.parent/'.pending-unfinished').write_bytes(b'pending')
        (original.parent/('c'*43)).write_bytes(b'orphan not in snapshot')
        backup=self.destination()
        report=self.tool.copy_database(self.source,backup)
        self.assertEqual(report['materialFiles']['count'],2)
        self.assertEqual(report['materialFiles']['bytes'],sum(p.stat().st_size for p in original.parent.iterdir() if p.name in {'a'*43,'b'*43}))
        restored=self.destination('restored')
        self.tool.copy_database(backup,restored)
        for database in (backup,restored):
            materials=database.parent/'material-uploads'
            self.assertEqual({p.name for p in materials.iterdir()},{'a'*43,'b'*43})
            for path in materials.iterdir():
                self.assertEqual(path.read_bytes(),(original.parent/path.name).read_bytes())
                if os.name!='nt':self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        with self.assertRaises(FileExistsError):self.tool.copy_database(backup,restored)

    def test_uses_backed_up_database_rows_even_when_new_upload_completes_during_backup(self):
        self.material()
        original=self.tool.copy_material_uploads
        def concurrent_upload(database,source,destination):
            self.material('b',b'A newer upload outside the DB snapshot')
            return original(database,source,destination)
        target=self.destination()
        with patch.object(self.tool,'copy_material_uploads',side_effect=concurrent_upload):
            report=self.tool.copy_database(self.source,target)
        self.assertEqual(report['materialFiles']['count'],1)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute('SELECT id FROM material_uploads').fetchall(),[('a'*43,)])
        self.assertEqual({p.name for p in (target.parent/'material-uploads').iterdir()},{'a'*43})

    def test_missing_or_corrupt_source_fails_and_cleans_new_database_and_partial_files(self):
        first=self.material()
        second=self.material('b',b'original')
        for failure in ('missing','wrong-size','wrong-hash'):
            with self.subTest(failure=failure):
                second.write_bytes(b'original')
                if failure=='missing':second.unlink()
                elif failure=='wrong-size':second.write_bytes(b'bad')
                else:second.write_bytes(b'altered!')
                target=self.destination(failure)
                with self.assertRaises((OSError,ValueError)):
                    self.tool.copy_database(self.source,target)
                self.assertFalse(target.exists())
                self.assertEqual(list((target.parent/'material-uploads').iterdir()),[])
                self.assertTrue(first.exists())

    def test_existing_identical_file_is_verified_and_different_file_is_never_overwritten(self):
        original=self.material()
        target=self.destination()
        directory=target.parent/'material-uploads'
        directory.mkdir(parents=True)
        existing=directory/original.name
        existing.write_bytes(original.read_bytes())
        self.tool.copy_database(self.source,target)
        failed=self.destination('conflict')
        other=failed.parent/'material-uploads'/original.name
        other.parent.mkdir(parents=True)
        other.write_bytes(b'Do not overwrite')
        with self.assertRaisesRegex(ValueError,'Refusing to overwrite'):
            self.tool.copy_database(self.source,failed)
        self.assertEqual(other.read_bytes(),b'Do not overwrite')
        self.assertFalse(failed.exists())

    def test_path_metadata_cannot_escape_material_directory(self):
        self.material()
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE material_uploads SET id='../outside'")
            db.commit()
        target=self.destination()
        with self.assertRaisesRegex(ValueError,'Invalid material metadata'):
            self.tool.copy_database(self.source,target)
        self.assertFalse(target.exists())

    def symlink(self,target,link,directory=False):
        try:link.symlink_to(target,target_is_directory=directory)
        except (OSError,NotImplementedError) as error:self.skipTest('Symlinks unavailable: '+str(error))

    def test_source_file_symlink_is_rejected_even_when_content_matches(self):
        original=self.material()
        separate=self.root/'separate'
        separate.write_bytes(original.read_bytes())
        original.unlink()
        self.symlink(separate,original)
        target=self.destination()
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.tool.copy_database(self.source,target)
        self.assertFalse(target.exists())

    def test_destination_directory_symlink_does_not_write_outside_destination(self):
        self.material()
        target=self.destination()
        target.parent.mkdir()
        outside=self.root/'outside'
        outside.mkdir()
        self.symlink(outside,target.parent/'material-uploads',True)
        with self.assertRaisesRegex(ValueError,'real directory'):
            self.tool.copy_database(self.source,target)
        self.assertEqual(list(outside.iterdir()),[])
        self.assertFalse(target.exists())

    def test_legacy_database_without_upload_table_still_backs_up(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('DROP TABLE material_uploads')
            db.execute('CREATE TABLE legacy (id INTEGER)')
            db.commit()
        report=self.tool.copy_database(self.source,self.destination())
        self.assertEqual(report['materialFiles'],{'count':0,'bytes':0})


if __name__=='__main__':unittest.main()
