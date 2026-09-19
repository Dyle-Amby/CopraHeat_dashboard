import unittest

from hardware.sensors import parse_milli, parse_w1_slave, read_dht22, read_ds18b20

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
CRC_FAIL = "ff ff ff ff ff ff ff ff ff : crc=c9 NO\nff ff ff ff ff ff ff ff ff t=-62"


class W1SlaveParserTest(unittest.TestCase):
    def test_parses_good_reading(self):
        self.assertEqual(parse_w1_slave(GOOD), 23.125)

    def test_negative_temperature(self):
        self.assertEqual(parse_w1_slave(GOOD.replace("t=23125", "t=-5250")), -5.25)

    def test_crc_failure_is_none(self):
        self.assertIsNone(parse_w1_slave(CRC_FAIL))

    def test_power_on_value_passes_through(self):
        # 85.0 is left for the controllers to reject via max_temp
        self.assertEqual(parse_w1_slave(GOOD.replace("t=23125", "t=85000")), 85.0)

    def test_truncated_input_is_none(self):
        self.assertIsNone(parse_w1_slave(""))
        self.assertIsNone(parse_w1_slave("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES"))

    def test_missing_t_field_is_none(self):
        self.assertIsNone(parse_w1_slave("x : crc=57 YES\ngarbage"))

    def test_non_numeric_is_none(self):
        self.assertIsNone(parse_w1_slave(GOOD.replace("t=23125", "t=abc")))


class MilliParserTest(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(parse_milli("45200\n"), 45.2)
        self.assertEqual(parse_milli("-1500"), -1.5)

    def test_garbage_is_none(self):
        self.assertIsNone(parse_milli(""))
        self.assertIsNone(parse_milli("nan"))


class ReadersWithoutHardwareTest(unittest.TestCase):
    """On a machine with no /sys drivers the readers must fail closed, not raise."""

    def test_ds18b20_returns_none(self):
        self.assertIsNone(read_ds18b20())

    def test_dht22_returns_none_pair(self):
        self.assertEqual(read_dht22(), (None, None))


if __name__ == "__main__":
    unittest.main()
