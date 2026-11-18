import unittest
from app.capture_worker import twitch_channel_from_url

class TwitchCaptureTests(unittest.TestCase):
    def test_channel(self):
        self.assertEqual(twitch_channel_from_url('https://www.twitch.tv/papaplatte'), 'papaplatte')
        self.assertEqual(twitch_channel_from_url('https://twitch.tv/Papaplatte'), 'papaplatte')
    def test_rejects_other_urls(self):
        for url in ('https://evil.example/papaplatte', 'http://twitch.tv/papaplatte',
                    'https://twitch.tv/papaplatte/videos',
                    'https://twitch.tv/papaplatte?something=1'):
            self.assertIsNone(twitch_channel_from_url(url))

if __name__ == '__main__': unittest.main()
