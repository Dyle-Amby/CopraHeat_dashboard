import unittest

from hardware.climate import BatchState, FanController, HeaterController, HysteresisBand

IDLE, DRYING, COOLDOWN = BatchState.IDLE, BatchState.DRYING, BatchState.COOLDOWN


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


class FanControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctl = FanController(interlock_temp=60.0, overtemp=75.0, dwell_s=30.0)

    def test_starts_off(self):
        self.assertFalse(self.ctl.fans_on)

    def test_state_rules(self):
        self.assertFalse(self.ctl.update(IDLE, 65.0, now=0))
        self.assertFalse(self.ctl.update(DRYING, 59.9, now=100))
        self.assertTrue(self.ctl.update(DRYING, 60.0, now=200))
        self.assertTrue(self.ctl.update(COOLDOWN, 40.0, now=300))
        self.assertFalse(self.ctl.update(IDLE, 40.0, now=400))

    def test_dwell_blocks_change_then_allows_it(self):
        self.ctl.update(DRYING, 65.0, now=0)
        self.assertTrue(self.ctl.update(DRYING, 55.0, now=29.9))
        self.assertFalse(self.ctl.update(DRYING, 55.0, now=30.0))

    def test_first_switch_is_not_dwell_blocked(self):
        self.assertTrue(self.ctl.update(DRYING, 65.0, now=5.0))

    def test_chatter_around_interlock_is_suppressed(self):
        switches = 0
        prev = self.ctl.fans_on
        for i in range(60):  # 5 minutes, sampled every 5 s, temp wobbling 59 <-> 61
            temp = 61.0 if i % 2 == 0 else 59.0
            state = self.ctl.update(DRYING, temp, now=i * 5.0)
            switches += state != prev
            prev = state
        # every change requires a full 30 s dwell, so at most one per 30 s window
        self.assertLessEqual(switches, 300 // 30 + 1)

    def test_overtemp_forces_on_in_idle(self):
        self.assertTrue(self.ctl.update(IDLE, 76.0, now=0))
        self.assertTrue(self.ctl.overtemp_active)

    def test_overtemp_bypasses_dwell(self):
        self.ctl.update(DRYING, 65.0, now=0)
        self.ctl.update(DRYING, 55.0, now=30)  # OFF at t=30
        self.assertFalse(self.ctl.fans_on)
        self.assertTrue(self.ctl.update(DRYING, 75.1, now=32))  # only 2 s later

    def test_overtemp_threshold_is_strict(self):
        self.assertFalse(self.ctl.update(IDLE, 75.0, now=0))
        self.assertFalse(self.ctl.overtemp_active)

    def test_overtemp_clearing_respects_dwell(self):
        self.ctl.update(IDLE, 76.0, now=0)
        self.assertTrue(self.ctl.update(IDLE, 70.0, now=10))
        self.assertFalse(self.ctl.overtemp_active)
        self.assertFalse(self.ctl.update(IDLE, 70.0, now=30))

    def test_overtemp_while_already_on_does_not_reset_dwell(self):
        self.ctl.update(DRYING, 65.0, now=0)   # ON at t=0
        self.ctl.update(DRYING, 76.0, now=10)  # already on; no state change
        self.assertFalse(self.ctl.update(IDLE, 50.0, now=30))  # dwell counted from t=0

    def test_sensor_fault_while_drying_keeps_air_moving(self):
        self.assertTrue(self.ctl.update(DRYING, None, now=0))
        self.assertFalse(self.ctl.overtemp_active)

    def test_sensor_fault_while_idle_stays_off(self):
        self.assertFalse(self.ctl.update(IDLE, None, now=0))

    def test_full_batch_switches_relay_twice(self):
        t = 0.0
        switches = 0
        prev = self.ctl.fans_on

        def step(state, temp):
            nonlocal t, switches, prev
            t += 10.0
            on = self.ctl.update(state, temp, now=t)
            switches += on != prev
            prev = on

        for temp in range(30, 60, 2):      # heat-up: fans stay off
            step(DRYING, float(temp))
        for temp in (60, 63, 66, 69, 70, 67, 62, 60, 64, 68):  # drying: fans on
            step(DRYING, float(temp))
        for temp in (68, 60, 50, 42, 35):  # cooldown: fans on
            step(COOLDOWN, float(temp))
        step(IDLE, 34.0)                   # done: fans off

        self.assertEqual(switches, 2)
        self.assertFalse(self.ctl.fans_on)

    def test_rejects_interlock_at_or_above_overtemp(self):
        with self.assertRaises(ValueError):
            FanController(interlock_temp=75.0, overtemp=75.0)


if __name__ == "__main__":
    unittest.main()
