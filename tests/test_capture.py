import datetime as dt
import tempfile
import unittest
from pathlib import Path
from app.capture import (UTC, segment_candidates, delete_old_segments,
                         ffmpeg_record_cmd, parse_segment_time, require_authorized_capture)
from unittest.mock import patch

class CaptureTests(unittest.TestCase):
    def test_segment_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for s in [0,2,4,6,8,10,12,14]:
                (folder / f'seg_20261118T1200{s:02d}.ts').touch()
            at=dt.datetime(2026,11,18,12,0,7,tzinfo=UTC)
            files=segment_candidates(folder,at-dt.timedelta(seconds=3),at+dt.timedelta(seconds=3),margin=0)
            self.assertEqual([f.name for f in files],['seg_20261118T120004.ts','seg_20261118T120006.ts', 'seg_20261118T120008.ts','seg_20261118T120010.ts'])
    def test_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            old=folder/'seg_20261118T120000.ts'; old.touch()
            recent=folder/'seg_20261118T120450.ts'; recent.touch()
            delete_old_segments(folder,retain_seconds=180,now=dt.datetime(2026,11,18,12,5,tzinfo=UTC))
            self.assertFalse(old.exists());self.assertTrue(recent.exists())
    def test_invalid_source(self):
        with self.assertRaises(ValueError):ffmpeg_record_cmd('twitch.tv/channel',Path('/tmp/demo'))
    def test_no_permission_fails_closed(self):
        with patch.dict('os.environ',{'CAPTURE_PERMISSION_CONFIRMED':'false'}):
            with self.assertRaises(RuntimeError):require_authorized_capture()

if __name__=='__main__':unittest.main()
