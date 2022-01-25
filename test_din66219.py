import unittest

from ctrlcode import SOH, ETX
from din66219 import append_bcc, check_bcc


class BccTestCase(unittest.TestCase):
    def test_append_bcc(self):
        self.assertEqual(
            append_bcc(f'{SOH}B0{ETX}'),
            b'\x01B0\x03q')
        self.assertEqual(
            append_bcc(f'aaaaa{SOH}B0{ETX}'),
            b'aaaaa\x01B0\x03q')

    def test_append_bcc_excess(self):
        with self.assertRaises(ValueError):
            append_bcc(f'{SOH}B0{ETX}x')

    def test_append_bcc_little(self):
        with self.assertRaises(ValueError):
            append_bcc(f'{SOH}B0')

    def test_check_bcc(self):
        check_bcc(f'{SOH}B0{ETX}q')
        check_bcc(f'aaaaa{SOH}B0{ETX}q')

    def test_check_bcc_bad(self):
        with self.assertRaises(ValueError):
            check_bcc(f'{SOH}B0{ETX}r')

    def test_check_bcc_excess(self):
        with self.assertRaises(ValueError):
            check_bcc(f'{SOH}B0{ETX}qq')

    def test_check_bcc_little(self):
        with self.assertRaises(ValueError):
            check_bcc(f'{SOH}B0{ETX}')
