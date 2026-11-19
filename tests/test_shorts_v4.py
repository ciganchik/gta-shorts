import unittest
import tempfile
from pathlib import Path
from app.shorts_v4 import (validate_layout,crop_params,write_ass,ass_escape,ffmpeg_vertical_cmd)

class ShortsV4Tests(unittest.TestCase):
    def test_no_false_facecam(self):
        self.assertIsNone(validate_layout({'focus_x':0.6,'facecam':{'x':0.8,'y':0.02,'w':0.2,'h':0.3,'confidence':0.3}})['facecam'])
        self.assertIsNone(validate_layout({'focus_x':0.5,'facecam':{'x':0,'y':0,'w':1,'h':1,'confidence':1}})['facecam'])
    def test_true_facecam(self):
        a=validate_layout({'focus_x':1.5,'facecam':{'x':0.79,'y':0.03,'w':0.19,'h':0.2,'confidence':0.98}})
        self.assertIsNotNone(a['facecam'])
        self.assertEqual(a['focus_x'],.88)
    def test_crop_in_bounds(self):
        for target in ((720,1280),(720,1000)):
            w,h,x,y=crop_params(1920,1080,.8,*target)
            self.assertGreater(w,0);self.assertGreater(h,0)
            self.assertLessEqual(x+w,1920);self.assertLessEqual(y+h,1080)
    def test_subtitle_escape(self):
        self.assertNotIn('{',ass_escape(r'{\pos(1,2)} text'))
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'captions.ass'
            self.assertTrue(write_ass([{'start':0.,'end':2.,'text':'Привет из игры!'}],out,True))
            self.assertIn('Привет из игры',out.read_text(encoding='utf-8'))
    def test_filter_graph(self):
        cmd=ffmpeg_vertical_cmd(Path('in.mp4'),Path('out.mp4'),(1920,1080),
               {'focus_x':.5,'facecam':{'x':.75,'y':.02,'w':.22,'h':.25}},None,30)
        self.assertIn('overlay=',cmd[cmd.index('-filter_complex')+1])
        self.assertIn('720:1000',cmd[cmd.index('-filter_complex')+1])

if __name__=='__main__':unittest.main()
