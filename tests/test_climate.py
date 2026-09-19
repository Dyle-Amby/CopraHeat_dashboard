import unittest

from hardware.climate import HeaterController, HysteresisBand


class HeaterControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctl = HeaterController(HysteresisBand(60.0, 70.0), max_temp=80.0)

    def test_starts_off(self):
        self.assertFalse(self.ctl.heater_on)

    def test_turns_on_at_or_below_low(self):
        self.assertTrue(self.ctl.update(60.0))
        self.ctl.heater_on = False
        self.assertTrue(self.ctl.update(42.0))

    def test_turns_off_at_or_above_high(self):
        self.ctl.update(50.0)
        self.assertFalse(self.ctl.update(70.0))
        self.ctl.update(50.0)
        self.assertFalse(self.ctl.update(75.0))

    def test_holds_state_inside_band(self):
        self.ctl.update(50.0)
        for t in (61.0, 65.0, 69.9):
            self.assertTrue(self.ctl.update(t), f"should stay ON at {t}")
        self.ctl.update(70.0)
        for t in (69.9, 65.0, 60.1):
            self.assertFalse(self.ctl.update(t), f"should stay OFF at {t}")

    def test_full_cycle(self):
        trace = [55, 62, 68, 70, 66, 61, 60, 63, 71, 65]
        expected = [1, 1, 1, 0, 0, 0, 1, 1, 0, 0]
        self.assertEqual([int(self.ctl.update(t)) for t in trace], expected)

    def test_sensor_fault_forces_off(self):
        self.ctl.update(50.0)
        self.assertFalse(self.ctl.update(None))
        self.assertTrue(self.ctl.fault)

    def test_overtemp_forces_off(self):
        self.ctl.update(50.0)
        self.assertFalse(self.ctl.update(80.0))
        self.assertTrue(self.ctl.fault)
        self.assertFalse(self.ctl.update(85.0))

    def test_recovers_after_fault(self):
        self.ctl.update(None)
        self.assertTrue(self.ctl.update(55.0))
        self.assertFalse(self.ctl.fault)

    def test_ds18b20_power_on_value_is_safe(self):
        # 85.0 is what a DS18B20 reports before its first real conversion
        self.ctl.update(50.0)
        self.assertFalse(self.ctl.update(85.0))

    def test_rejects_inverted_band(self):
        with self.assertRaises(ValueError):
            HysteresisBand(70.0, 60.0)

    def test_rejects_band_reaching_max_temp(self):
        with self.assertRaises(ValueError):
            HeaterController(HysteresisBand(60.0, 80.0), max_temp=80.0)


if __name__ == "__main__":
    unittest.main()
