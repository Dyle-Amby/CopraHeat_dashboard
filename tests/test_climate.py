import unittest

from hardware.climate import FanController, HeaterController, HysteresisBand
from hardware.states import BatchState

IDLE, DRYING, COOLDOWN = BatchState.IDLE, BatchState.DRYING, BatchState.COOLDOWN


class HeaterControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctl = HeaterController(HysteresisBand(60.0, 70.0), max_temp=80.0)

    def drying(self, temp):
        return self.ctl.update(DRYING, temp)

    def test_starts_off(self):
        self.assertFalse(self.ctl.heater_on)

    def test_turns_on_at_or_below_low(self):
        self.assertTrue(self.drying(60.0))
        self.ctl.heater_on = False
        self.assertTrue(self.drying(42.0))

    def test_turns_off_at_or_above_high(self):
        self.drying(50.0)
        self.assertFalse(self.drying(70.0))
        self.drying(50.0)
        self.assertFalse(self.drying(75.0))

    def test_holds_state_inside_band(self):
        self.drying(50.0)
        for t in (61.0, 65.0, 69.9):
            self.assertTrue(self.drying(t), f"should stay ON at {t}")
        self.drying(70.0)
        for t in (69.9, 65.0, 60.1):
            self.assertFalse(self.drying(t), f"should stay OFF at {t}")

    def test_full_cycle(self):
        trace = [55, 62, 68, 70, 66, 61, 60, 63, 71, 65]
        expected = [1, 1, 1, 0, 0, 0, 1, 1, 0, 0]
        self.assertEqual([int(self.drying(t)) for t in trace], expected)

    def test_never_heats_when_idle(self):
        self.assertFalse(self.ctl.update(IDLE, 30.0))
        self.assertFalse(self.ctl.fault)

    def test_never_heats_during_cooldown(self):
        self.drying(50.0)
        self.assertFalse(self.ctl.update(COOLDOWN, 50.0))

    def test_leaving_drying_cuts_heater_immediately(self):
        self.assertTrue(self.drying(50.0))
        self.assertFalse(self.ctl.update(IDLE, 50.0))

    def test_sensor_fault_forces_off(self):
        self.drying(50.0)
        self.assertFalse(self.drying(None))
        self.assertTrue(self.ctl.fault)

    def test_overtemp_forces_off(self):
        self.drying(50.0)
        self.assertFalse(self.drying(80.0))
        self.assertTrue(self.ctl.fault)
        self.assertFalse(self.drying(85.0))

    def test_recovers_after_fault(self):
        self.drying(None)
        self.assertTrue(self.drying(55.0))
        self.assertFalse(self.ctl.fault)

    def test_ds18b20_power_on_value_is_safe(self):
        # 85.0 is what a DS18B20 reports before its first real conversion
        self.drying(50.0)
        self.assertFalse(self.drying(85.0))

    def test_rejects_inverted_band(self):
        with self.assertRaises(ValueError):
            HysteresisBand(70.0, 60.0)

    def test_rejects_band_reaching_max_temp(self):
        with self.assertRaises(ValueError):
            HeaterController(HysteresisBand(60.0, 80.0), max_temp=80.0)


class FanControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctl = FanController(
            interlock_temp=60.0,
            overtemp=75.0,
            dwell_s=30.0,
            warmup_s=1200.0,
            pulse_on_s=60.0,
            pulse_period_s=300.0,
            rh_on=60.0,
            rh_off=45.0,
        )

    def test_starts_off(self):
        self.assertFalse(self.ctl.fans_on)
        self.assertEqual(self.ctl.reason, "off")

    def test_fans_stay_off_during_warmup_when_temp_below_interlock(self):
        # 10 minutes into DRYING (< 1200 s warmup), temp=55 C (< 60 C interlock)
        self.assertFalse(self.ctl.update(DRYING, 55.0, now=0))
        self.assertEqual(self.ctl.reason, "warmup")
        self.assertFalse(self.ctl.update(DRYING, 58.0, now=600))
        self.assertEqual(self.ctl.reason, "warmup")
        self.assertFalse(self.ctl.update(DRYING, 59.9, now=1199))
        self.assertEqual(self.ctl.reason, "warmup")

    def test_pulses_start_after_warmup_at_right_times(self):
        # warmup ends at t=1200. First pulse is [1200, 1260) within [1200, 1500)
        # t=0..1199: warmup
        self.ctl.update(DRYING, 55.0, now=0)
        self.assertFalse(self.ctl.fans_on)

        # t=1200: pulse starts
        self.assertTrue(self.ctl.update(DRYING, 55.0, now=1200))
        self.assertEqual(self.ctl.reason, "pulse")

        # t=1259: pulse continues
        self.assertTrue(self.ctl.update(DRYING, 55.0, now=1259))
        self.assertEqual(self.ctl.reason, "pulse")

        # t=1260: pulse ends, hold starts (now - last_change = 60s >= dwell_s 30s)
        self.assertFalse(self.ctl.update(DRYING, 55.0, now=1260))
        self.assertEqual(self.ctl.reason, "hold")

        # t=1400: still in hold
        self.assertFalse(self.ctl.update(DRYING, 55.0, now=1400))
        self.assertEqual(self.ctl.reason, "hold")

        # t=1500: second pulse starts (1200 + 300)
        self.assertTrue(self.ctl.update(DRYING, 55.0, now=1500))
        self.assertEqual(self.ctl.reason, "pulse")

    def test_humidity_latch_turns_on_at_rh_on_and_off_at_rh_off(self):
        # After warmup (t=1200), during pulse hold (e.g. t=1300), humidity override turns fans ON if rh >= 60
        self.ctl.update(DRYING, 50.0, now=0)
        # At t=1300: warmup is done, outside pulse window ([1200, 1260)), so demand without humidity is hold (off)
        self.assertFalse(self.ctl.update(DRYING, 50.0, now=1300, rh=55.0))
        self.assertEqual(self.ctl.reason, "hold")

        # At t=1335: RH hits 60.0 -> latch turns on
        self.assertTrue(self.ctl.update(DRYING, 50.0, now=1335, rh=60.0))
        self.assertEqual(self.ctl.reason, "humidity")

        # In between rh_off (45) and rh_on (60), e.g. at 50% -> stays latched ON
        self.assertTrue(self.ctl.update(DRYING, 50.0, now=1370, rh=50.0))
        self.assertEqual(self.ctl.reason, "humidity")

        # Below rh_off (45), e.g. 44.9% -> latch drops, reverts to hold (fans off)
        self.assertFalse(self.ctl.update(DRYING, 50.0, now=1405, rh=44.9))
        self.assertEqual(self.ctl.reason, "hold")

        # In between again (e.g. 50%), stays unlatched (fans off)
        self.assertFalse(self.ctl.update(DRYING, 50.0, now=1440, rh=50.0))
        self.assertEqual(self.ctl.reason, "hold")

    def test_temp_at_or_above_interlock_forces_fans_on_even_during_warmup(self):
        # Even at t=10 (warmup), temp >= 60.0 forces fans ON
        self.assertTrue(self.ctl.update(DRYING, 60.0, now=10))
        self.assertEqual(self.ctl.reason, "temp")
        self.assertTrue(self.ctl.update(DRYING, 65.0, now=50))
        self.assertEqual(self.ctl.reason, "temp")

    def test_overtemp_bypasses_dwell(self):
        self.ctl.update(DRYING, 65.0, now=0)   # fans ON at t=0
        self.ctl.update(DRYING, 55.0, now=30)  # fans OFF at t=30
        self.assertFalse(self.ctl.fans_on)
        # Only 2 s later (< 30s dwell): overtemp 75.1 C forces ON immediately
        self.assertTrue(self.ctl.update(DRYING, 75.1, now=32))
        self.assertEqual(self.ctl.reason, "overtemp")
        self.assertTrue(self.ctl.overtemp_active)

    def test_none_rh_skips_humidity_rule_and_none_temp_turns_fans_on(self):
        # None RH: skips humidity rule, follows warmup / pulses / temp
        self.ctl.update(DRYING, 50.0, now=0, rh=None)
        self.assertFalse(self.ctl.fans_on)
        self.assertEqual(self.ctl.reason, "warmup")

        # None temp: sensor fault forces fans ON even during warmup
        self.assertTrue(self.ctl.update(DRYING, None, now=35, rh=None))
        self.assertEqual(self.ctl.reason, "sensor-fault")

    def test_state_leaving_drying_resets_warmup_timer(self):
        # DRYING for 1100 s
        self.ctl.update(DRYING, 50.0, now=0)
        self.ctl.update(DRYING, 50.0, now=1100)
        self.assertEqual(self.ctl.reason, "warmup")

        # Transition out to COOLDOWN (or IDLE)
        self.ctl.update(COOLDOWN, 35.0, now=1200)
        self.assertEqual(self.ctl.reason, "cooldown")

        # Re-entering DRYING resets the timer: warmup starts again at t=1300
        self.ctl.update(DRYING, 50.0, now=1300)
        self.assertEqual(self.ctl.reason, "warmup")
        self.assertFalse(self.ctl.fans_on)
        # 500 s later (t=1800): only 500 s into new DRYING session (< 1200s), still warmup
        self.ctl.update(DRYING, 50.0, now=1800)
        self.assertEqual(self.ctl.reason, "warmup")
        self.assertFalse(self.ctl.fans_on)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            FanController(interlock_temp=75.0, overtemp=75.0)
        with self.assertRaises(ValueError):
            FanController(rh_on=50.0, rh_off=50.0)
        with self.assertRaises(ValueError):
            FanController(rh_on=40.0, rh_off=50.0)
        with self.assertRaises(ValueError):
            FanController(dwell_s=30.0, pulse_on_s=20.0, pulse_period_s=300.0)
        with self.assertRaises(ValueError):
            FanController(dwell_s=30.0, pulse_on_s=300.0, pulse_period_s=300.0)


if __name__ == "__main__":
    unittest.main()
