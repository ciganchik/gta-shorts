import unittest, datetime as dt
from app.core import SpikeDetector, normalize_channel
class TestSpike(unittest.TestCase):
    def test_channel(self):
        self.assertEqual(normalize_channel('https://www.twitch.tv/Test_Channel'), 'test_channel')
        with self.assertRaises(ValueError):normalize_channel('https://evil.com/')
    def test_spike(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        data=[(now+dt.timedelta(seconds=10*i), ['u1','u2']*2) for i in range(12)]
        data.append((now+dt.timedelta(seconds=120),[f'u{i%15}' for i in range(40)]))
        r=SpikeDetector().evaluate(data)
        self.assertTrue(r)
        self.assertEqual(r['count_10s'],40)
    def test_spam(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        data=[(now+dt.timedelta(seconds=10*i),['u1']*2) for i in range(8)]
        data.append((now+dt.timedelta(seconds=80),['bot']*50))
        self.assertIsNone(SpikeDetector().evaluate(data))
if __name__=='__main__':unittest.main()
