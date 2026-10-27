(function () {
    function setText(id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val ?? '—';
    }

    function setBadge(id, isResponding) {
        const el = document.getElementById(id);
        if (!el) return;
        if (isResponding === true) {
            el.textContent = 'Responding';
            el.className = 'badge bg-success';
        } else if (isResponding === false) {
            el.textContent = 'Not responding';
            el.className = 'badge bg-danger';
        } else {
            el.textContent = '—';
            el.className = 'badge bg-secondary';
        }
    }

    function formatOutputLast(lastCmd, lastAt) {
        if (!lastCmd) return 'Last: —';
        const timeStr = lastAt ? new Date(lastAt).toLocaleTimeString() : '';
        return `Last: ${lastCmd}${timeStr ? ' @ ' + timeStr : ''}`;
    }

    function render(live) {
        const sup = live.supervisor ?? 'stopped';
        const dim = (sup !== 'running');
        const container = document.getElementById('componentTabContent');
        if (container) container.classList.toggle('opacity-50', dim);

        let comp = {};
        if (typeof live.components === 'object' && live.components !== null) {
            comp = live.components;
        } else if (typeof live.components === 'string') {
            try { comp = JSON.parse(live.components); } catch (_) { }
        }

        // --- 1. Hopper ---
        const hopperCellOk = comp.hopper_scale?.status === 'ok' || live.hopper_kg !=
            null;
        setBadge('h-hopper-cell', hopperCellOk);
        setText('h-hopper-kg', live.hopper_kg != null ? live.hopper_kg.toFixed(3) :
            '—');
        setText('h-gate-state', live.gate_open ? 'Open' : 'Closed');
        const servo = comp.servo ?? {};
        setText('h-servo-last', formatOutputLast(servo.last_cmd, servo.last_cmd_at));

        // --- 2. Conveyor ---
        const conv = comp.conveyor ?? {};
        setText('c-stepper-last', formatOutputLast(conv.last_cmd, conv.last_cmd_at));
        setText('c-motion-state', conv.busy ? 'Busy (moving)' : 'Idle');
        setText('c-last-cmd', conv.last_result ?? '—');
        setText('c-position', conv.position_steps ?? '—');

        // --- 3. Carriage ---
        const car = comp.carriage ?? {};
        setText('car-stepper-last', formatOutputLast(car.last_cmd, car.last_cmd_at));
        setText('car-bin', car.current_bin ? car.current_bin.toUpperCase() : 'Unknown');
        setText('car-last-trigger', car.last_trigger ?? '—');

        // Limit Switches (NC with pull-ups)
        const sw = comp.switches ?? {};
        function switchText(pressed) {
            if (pressed === true) return 'Pressed';
            if (pressed === false) return 'Open';
            return '—';
        }
        setText('car-ls1', switchText(sw.LS1));
        setText('car-ls2', switchText(sw.LS2));
        setText('car-ls3', switchText(sw.LS3));

        // Bin Load Cells
        const bins = comp.bin_kg ?? {};
        const binStatus = comp.bin_scales_status ?? {};
        setBadge('car-hx-great', binStatus.great === 'ok');
        setBadge('car-hx-good', binStatus.good === 'ok');
        setBadge('car-hx-bad', binStatus.bad === 'ok');
        setText('car-bin-great', bins.great != null ? bins.great.toFixed(3) : '—');
        setText('car-bin-good', bins.good != null ? bins.good.toFixed(3) : '—');
        setText('car-bin-bad', bins.bad != null ? bins.bad.toFixed(3) : '—');

        // --- 4. Drying Chamber ---
        const chamberOk = live.chamber_c != null || comp.chamber_sensor?.status ===
            'ok';
        const exhaustOk = live.exhaust_c != null || comp.exhaust_sensor?.status ===
            'ok';
        setBadge('ch-ds-state', chamberOk);
        setText('ch-temp', live.chamber_c != null ? live.chamber_c.toFixed(1) : '—');

        setBadge('ch-dht-state', exhaustOk);
        const exh = (live.exhaust_c != null)
            ? `${live.exhaust_c.toFixed(1)} °C / ${live.exhaust_rh != null ?
                live.exhaust_rh.toFixed(0) + '%' : '— RH'}`
            : '—';
        setText('ch-exhaust', exh);

        setText('ch-heater', live.heater_on ? 'ON' : 'OFF');
        const heater = comp.heater ?? {};
        setText('ch-heater-last', formatOutputLast(heater.last_cmd,
            heater.last_cmd_at));

        setText('ch-fans', live.fans_on ? `ON (${live.fan_reason ?? 'forced'})` :
            'OFF');
        const fans = comp.fans ?? {};
        setText('ch-fans-last', formatOutputLast(fans.last_cmd, fans.last_cmd_at));

        // --- 5. Camera ---
        const cam = comp.camera ?? {};
        const camOk = cam.status === 'ok' || cam.ok === true;
        setBadge('cam-state', camOk);
        setText('cam-last', cam.last_capture_at ? new
            Date(cam.last_capture_at).toLocaleTimeString() : '—');

        // Calibration Lock Banner
        const lockBanner = document.getElementById('calLockBanner');
        if (lockBanner) lockBanner.classList.toggle('d-none', !live.calibration_locked);
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (window.CORDS && typeof window.CORDS.startLivePolling === 'function') {
            window.CORDS.startLivePolling(render);
        }
    });

    // Calibration and manual component controls
    async function toggleLock() {
        const banner = document.getElementById('calLockBanner');
        const isLocked = banner && !banner.classList.contains('d-none');
        const cmd = isLocked ? 'release_calibration_lock' : 'acquire_calibration_lock';
        const payload = isLocked ? {} : { duration_s: 600, component: 'hopper' };
        try {
            await window.CORDS.sendCommand(cmd, payload);
        } catch (err) {
            alert('Lock request failed: ' + err.message);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        const api = window.CORDS;
        if (!api || typeof api.sendCommand !== 'function') return;

        const bindCommand = (id, command, payload) => {
            const button = document.getElementById(id);
            if (button) button.addEventListener('click', () => api.sendCommand(command, payload));
        };

        const btnLockHopper = document.getElementById('btnLockHopper');
        if (btnLockHopper) btnLockHopper.addEventListener('click', toggleLock);

        const bindAsync = (id, action, successMessage) => {
            const button = document.getElementById(id);
            if (!button) return;
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    const res = await action();
                    alert(res?.result || successMessage);
                } catch (err) {
                    alert(err.message);
                } finally {
                    button.disabled = false;
                }
            });
        };

        bindAsync('btnTareClosed', () => api.sendCommand('tare_hopper', { gate: 'closed' }), 'Tare completed');
        bindAsync('btnCalScale', () => {
            const kg = parseFloat(document.getElementById('inputRefWeight')?.value) || 1.0;
            return api.sendCommand('calibrate_hopper_scale', { ref_weight_kg: kg });
        }, 'Scale calibrated');
        bindAsync('btnTareOpen', () => api.sendCommand('tare_hopper', { gate: 'open' }), 'Tare open completed');

        bindCommand('btnServoClose', 'set_hopper_servo', { state: 'closed' });
        bindCommand('btnServoOpen', 'set_hopper_servo', { state: 'open' });
        const sliderServo = document.getElementById('sliderServo');
        const servoAngleDisplay = document.getElementById('servoAngleDisplay');
        if (sliderServo && servoAngleDisplay) {
            sliderServo.addEventListener('input', event => {
                servoAngleDisplay.textContent = event.target.value + '°';
            });
        }
        const btnSetServoAngle = document.getElementById('btnSetServoAngle');
        if (btnSetServoAngle && sliderServo) {
            btnSetServoAngle.addEventListener('click', () =>
                api.sendCommand('set_hopper_servo', { angle: parseFloat(sliderServo.value) }));
        }

        bindCommand('btnCallGreat', 'goto_carriage_bin', { bin: 'great' });
        bindCommand('btnCallGood', 'goto_carriage_bin', { bin: 'good' });
        bindCommand('btnCallBad', 'goto_carriage_bin', { bin: 'bad' });
        bindCommand('btnHomeCarriage', 'home_carriage', {});

        ['great', 'good', 'bad'].forEach(bin => {
            const cap = bin.charAt(0).toUpperCase() + bin.slice(1);
            bindCommand(`btnTareBin${cap}`, 'calibrate_bin_scale', { bin, action: 'tare' });
            const calButton = document.getElementById(`btnCalBin${cap}`);
            const refInput = document.getElementById(`refKg${cap}`);
            if (calButton && refInput) {
                calButton.addEventListener('click', () => {
                    const kg = parseFloat(refInput.value) || 1.0;
                    api.sendCommand('calibrate_bin_scale', { bin, action: 'scale', ref_kg: kg });
                });
            }
        });
    });
})();