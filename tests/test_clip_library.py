import unittest
from fastapi import HTTPException
from app.clip_library import decode_storage_key

class ClipLibraryTests(unittest.TestCase):
    def test_valid_key(self):
        self.assertEqual(decode_storage_key('s3://gta-shorts-videos/clips/strogo/11.mp4', 'gta-shorts-videos', 'strogo'), 'clips/strogo/11.mp4')
    def test_reject_other_bucket(self):
        with self.assertRaises(ValueError):
            decode_storage_key('s3://other/clips/strogo/11.mp4','gta-shorts-videos','strogo')
    def test_reject_other_channel(self):
        with self.assertRaises(ValueError):
            decode_storage_key('s3://gta-shorts-videos/clips/ilame/11.mp4','gta-shorts-videos','strogo')
    def test_reject_traversal(self):
        with self.assertRaises(ValueError):
            decode_storage_key('s3://gta-shorts-videos/clips/strogo/../admin.mp4','gta-shorts-videos','strogo')
