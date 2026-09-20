import unittest

from hardware.hx711 import decode_24bit


class Decode24BitTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(decode_24bit(0), 0)

    def test_positive(self):
        self.assertEqual(decode_24bit(0x000001), 1)
        self.assertEqual(decode_24bit(0x123456), 0x123456)

    def test_negative(self):
        self.assertEqual(decode_24bit(0xFFFFFF), -1)
        self.assertEqual(decode_24bit(0xFFFFFE), -2)
        self.assertEqual(decode_24bit(0x800001), -(1 << 23) + 1)

    def test_rails_are_none(self):
        self.assertIsNone(decode_24bit(0x7FFFFF))
        self.assertIsNone(decode_24bit(0x800000))

    def test_ignores_bits_above_24(self):
        self.assertEqual(decode_24bit(0x1000005), 5)


if __name__ == "__main__":
    unittest.main()
