import importlib.util
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('publication',Path(__file__).resolve().parents[1]/'scripts/check_publication.py')
publication=importlib.util.module_from_spec(spec);spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def test_private_artifacts_are_rejected_even_if_tracked(self):
        names=['state/requests.jsonl','backups/config.toml','archive/old.py','auth.json','fixture.sqlite','secret.env/.env']
        self.assertEqual(len(publication.check_files(names)),len(names))

    def test_private_path_and_external_local_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            private='C:'+chr(92)+'Users'+chr(92)+'fixture-user'+chr(92)+'project'
            (root/'example.py').write_text(private,encoding='utf-8')
            (root/'README.md').write_text('[private]('+('../'*3)+'report.md)',encoding='utf-8')
            self.assertEqual(len(publication.check_files(['example.py','README.md'],root)),2)
            (root/'README.md').write_text('[public](docs/recovery.md)',encoding='utf-8')
            self.assertEqual(publication.check_files(['README.md'],root),[])


if __name__=='__main__':unittest.main()
