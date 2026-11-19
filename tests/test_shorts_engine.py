import unittest
from pathlib import Path
from app.shorts_engine import valid_output_key, render_cmd


class ShortsEngineTests(unittest.TestCase):
    def test_valid_key(self):
        self.assertTrue(valid_output_key('shorts/strogo/5.mp4','strogo',5))

    def test_invalid_key(self):
        self.assertFalse(valid_output_key('shorts/strogo/../../secret.mp4','strogo',5))
        self.assertFalse(valid_output_key('shorts/ilame/5.mp4','strogo',5))

    def test_render_geometry(self):
        command=render_cmd(Path('in.mp4'),Path('out.mp4'))
        self.assertIn('720:1280', ' '.join(command))
        self.assertIn('fps=30', ' '.join(command))
        self.assertEqual(command[-1], 'out.mp4')

if __name__=='__main__': unittest.main()
