import unittest

from obis import ElectricityObis


class ElectricityObisTestCase(unittest.TestCase):
    def test_1_8_0(self):
        obis = ElectricityObis.from_code('1.8.0')
        self.assertEqual(obis.code, '1.8.0')
        self.assertEqual(obis.unit, 'Wh')
        self.assertEqual(
            obis.description,
            'Positive active energy (A+) total')

    def test_2_8_4(self):
        obis = ElectricityObis.from_code('2.8.4')
        self.assertEqual(obis.code, '2.8.4')
        self.assertEqual(obis.unit, 'Wh')
        self.assertEqual(
            obis.description,
            'Negative active energy (A-) in T4')

    def test_16_7_0(self):
        obis = ElectricityObis.from_code('16.7.0')
        self.assertEqual(obis.code, '16.7.0')
        self.assertEqual(obis.unit, 'W')
        self.assertEqual(
            obis.description,
            'Sum active instantaneous power (A+ - A-)')

    def test_value_set_and_convert_and_stringify(self):
        obis = ElectricityObis.from_code('1.8.0').set_value(1234, 'kWh')
        self.assertEqual(str(obis.value), '1234000 Wh')
        self.assertEqual('%s' % (obis.value,), '1234000 Wh')
        self.assertEqual(f'{obis.value}', '1234000 Wh')

        with self.assertRaises(NotImplementedError):
            ElectricityObis.from_code('1.7.0').set_value(1234, 'kWh')

        obis = ElectricityObis.from_code('1.7.0').set_value(1234, 'kW')
        self.assertEqual(str(obis.value), '1234000 W')
        self.assertEqual('%s' % (obis.value,), '1234000 W')
        self.assertEqual(f'{obis.value}', '1234000 W')
