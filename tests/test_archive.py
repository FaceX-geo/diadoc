import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'archive.py'
spec = importlib.util.spec_from_file_location('diadoc_archive', MODULE)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = archive.connect(self.base)
        self.doc = {'boxId':'box','letterId':'letter','documentId':'doc','folder':'Inbox',
                    'name':'Документ.pdf','status':'Требуется подпись','counterparty':'Контрагент',
                    'fingerprint':'v1'}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_unchanged_metadata_keeps_archive_and_does_not_add_history(self):
        key,_ = archive.upsert(self.db,self.doc)
        self.db.execute('UPDATE documents SET archived_fingerprint=? WHERE key=?',(archive.fingerprint(self.doc),key))
        archive.upsert(self.db,self.doc)
        row = self.db.execute('SELECT * FROM documents').fetchone()
        self.assertEqual(row['archived_fingerprint'],row['fingerprint'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM history').fetchone()[0],1)

    def test_changed_metadata_preserves_previous_archive_and_adds_history(self):
        key,_ = archive.upsert(self.db,self.doc)
        original_fp=archive.fingerprint(self.doc)
        self.db.execute('UPDATE documents SET archived_fingerprint=? WHERE key=?',(original_fp,key))
        archive.upsert(self.db,{**self.doc,'status':'Подписан','fingerprint':'v2'})
        row=self.db.execute('SELECT * FROM documents').fetchone()
        self.assertEqual(row['archived_fingerprint'],original_fp)
        self.assertNotEqual(row['fingerprint'],original_fp)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM history').fetchone()[0],2)

    def test_full_scan_requires_contiguous_pages(self):
        source=self.base/'index.json'
        source.write_text(json.dumps({'run':'x','complete':True,'pages':[
          {'folder':'Inbox','page':2,'documents':[self.doc]}]}))
        with self.assertRaisesRegex(ValueError,'Non-contiguous'):
            archive.import_index(self.db,source,self.base)

    def test_empty_loading_page_is_not_an_empty_archive(self):
        source=self.base/'index.json'
        source.write_text(json.dumps({'run':'x','complete':True,'pages':[
          {'folder':'Internal','page':1,'documents':[]}]}))
        with self.assertRaisesRegex(ValueError,'explicit empty-state'):
            archive.import_index(self.db,source,self.base)

    def test_paths_cannot_escape_archive(self):
        for name in ('../secret','/absolute','x/../../secret','x\\..\\..\\secret'):
            with self.assertRaises(ValueError): archive.safe_path(name)

    def test_safari_cp866_names_recovered(self):
        self.assertEqual(archive.safari_name('П•з†в≠†п дЃађ†'),'Печатная форма')

    def test_visual_badges_do_not_trigger_download(self):
        a={**self.doc,'details':'Документ.pdf\nДоверенности'}
        b={**self.doc,'details':'Документ.pdf\n3\n\nДоверенности'}
        self.assertEqual(archive.fingerprint(a),archive.fingerprint(b))

if __name__=='__main__':unittest.main()
