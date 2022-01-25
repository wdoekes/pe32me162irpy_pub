import unittest

from ctrlcode import SOH, ETX


class CtrlcodeTestCase(unittest.TestCase):
    def test_fstring(self):
        self.assertEqual(f'{SOH}123{ETX}', '\x01123\x03')

    def test_repr(self):
        self.assertEqual(repr(SOH), '\x1b[1;34m^A\x1b[0m')

    def test_in(self):
        self.assertIn(1, (SOH, ETX))
        self.assertIn('\x03', (SOH, ETX))
        self.assertIn(b'\x03', (SOH, ETX))

    def test_int(self):
        self.assertEqual(SOH, 1)
        self.assertNotEqual(SOH, 2)

        self.assertNotEqual(ETX, 1)
        self.assertEqual(ETX, 3)

    def test_byte(self):
        self.assertEqual(SOH, b'\x01')
        self.assertNotEqual(SOH, b'\x02')

        self.assertNotEqual(ETX, b'\x01')
        self.assertEqual(ETX, b'\x03')

    def test_char(self):
        self.assertEqual(SOH, '\x01')
        self.assertNotEqual(SOH, '\x02')

        self.assertNotEqual(ETX, '\x01')
        self.assertEqual(ETX, '\x03')

    def test_must_be_one_byte(self):
        self.assertNotEqual(SOH, '\x01\x01')
        self.assertNotEqual(SOH, b'\x01\x01')
        self.assertNotEqual(ETX, '\x03\x00')
        self.assertNotEqual(ETX, b'\x03\x00')

    def test_substring(self):
        self.assertEqual(bytearray([1, 2, 3])[0], SOH)
        self.assertEqual(bytearray([1, 2, 3])[2], ETX)
        self.assertEqual(bytes(bytearray([1, 2, 3]))[0], SOH)
        self.assertEqual(bytes(bytearray([1, 2, 3]))[2], ETX)
