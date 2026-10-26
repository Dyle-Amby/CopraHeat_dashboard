// components.js — Phase 2 (Status only)
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
})();