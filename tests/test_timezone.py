# coding: utf-8
"""utility.timezone（pytz を置き換えた zoneinfo ベースの timezone 解決）。"""

import datetime
import unittest
import zoneinfo

import utility


class TimezoneTest(unittest.TestCase):
    def test_UTCは大文字小文字を区別せず標準のUTCを返す(self):
        for name in ('UTC', 'utc', 'Utc', None):
            with self.subTest(name=name):
                self.assertIs(datetime.timezone.utc, utility.timezone(name))

    def test_IANA名はZoneInfoになる(self):
        tokyo = utility.timezone('Asia/Tokyo')
        self.assertIsInstance(tokyo, zoneinfo.ZoneInfo)
        at_noon = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=tokyo)
        self.assertEqual(datetime.timedelta(hours=9), at_noon.utcoffset())
        self.assertEqual('2026/09/06 03:00', at_noon.astimezone(datetime.timezone.utc).strftime('%Y/%m/%d %H:%M'))

    def test_未知の名前は例外(self):
        with self.assertRaises(zoneinfo.ZoneInfoNotFoundError):
            utility.timezone('Mars/Olympus_Mons')

    def test_timestamp_pluginは未知のtimezoneでUTCへ退避する(self):
        from plugin import timestamp
        with self.assertLogs(level='ERROR'):
            runtime = timestamp.TimestampPlugin_Runtime({'timezone': 'Nowhere/Nothing'})
        self.assertIs(datetime.timezone.utc, runtime.timezone)
        tokyo = timestamp.TimestampPlugin_Runtime({'timezone': 'Asia/Tokyo'})
        self.assertEqual('Asia/Tokyo', str(tokyo.timezone))
        self.assertRegex(tokyo.runtime_object.date, r'^\d{4}/\d{2}/\d{2}$')


if __name__ == '__main__':
    unittest.main()
