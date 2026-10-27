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

    // Wizard State Tracking
    const wizardState = {
        currentLeg: 'hopper_to_chamber',
        baselinePos: null,
        currentPos: 0,
        trials: {
            hopper_to_chamber: [],
            chamber_to_camera: [],
            camera_to_discharge: []
        }
    };

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

        const settings = live.settings || {};

        // --- 1. Hopper ---
        const hopperCellOk = comp.hopper_scale?.status === 'ok' || live.hopper_kg != null;
        setBadge('h-hopper-cell', hopperCellOk);
        setText('h-hopper-kg', live.hopper_kg != null ? live.hopper_kg.toFixed(3) : '—');
        setText('h-gate-state', live.gate_open ? 'Open' : 'Closed');
        const servo = comp.servo ?? {};
        setText('h-servo-last', formatOutputLast(servo.last_cmd, servo.last_cmd_at));

        // --- 2. Conveyor ---
        const conv = comp.conveyor ?? {};
        wizardState.currentPos = conv.position_steps ?? 0;
        setText('c-stepper-last', formatOutputLast(conv.last_cmd, conv.last_cmd_at));
        setText('c-motion-state', conv.busy ? 'Busy (moving)' : 'Idle');
        setText('c-last-cmd', conv.last_result ?? '—');
        setText('c-position', conv.position_steps ?? '—');

        // Driver settings and RPM
        const p00 = parseFloat(settings.conveyor_p00 || '10000');
        const hz = parseFloat(settings.conveyor_hz || '400');
        const rpm = p00 > 0 ? ((hz * 60) / p00).toFixed(2) : '—';
        setText('c-speed-rpm', rpm);

        // Stale Calibration Indicator
        const isStale = settings.conveyor_cal_stale === '1';
        const staleAlert = document.getElementById('conveyorStaleAlert');
        const staleBadge = document.getElementById('conveyorStaleBadge');
        if (staleAlert) staleAlert.classList.toggle('d-none', !isStale);
        if (staleBadge) staleBadge.classList.toggle('d-none', !isStale);

        // Leg step counts
        setText('savedStepsLeg1', (settings.hopper_to_chamber_steps || '0') + ' steps');
        setText('savedStepsLeg2', (settings.chamber_to_camera_steps || '0') + ' steps');
        setText('savedStepsLeg3', (settings.camera_to_discharge_steps || '0') + ' steps');

        // Wizard Delta update
        if (wizardState.baselinePos !== null) {
            const delta = wizardState.currentPos - wizardState.baselinePos;
            setText('wizardDeltaSteps', delta);
        }

        // --- 3. Carriage ---
        const car = comp.carriage ?? {};
        setText('car-stepper-last', formatOutputLast(car.last_cmd, car.last_cmd_at));
        setText('car-bin', car.current_bin ? car.current_bin.toUpperCase() : 'Unknown');
        setText('car-last-trigger', car.last_trigger ?? '—');

        // Limit Switches
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
        const chamberOk = live.chamber_c != null || comp.chamber_sensor?.status === 'ok';
        const exhaustOk = live.exhaust_c != null || comp.exhaust_sensor?.status === 'ok';
        setBadge('ch-ds-state', chamberOk);
        setText('ch-temp', live.chamber_c != null ? live.chamber_c.toFixed(1) : '—');
        setBadge('ch-dht-state', exhaustOk);
        const exh = (live.exhaust_c != null)
            ? `${live.exhaust_c.toFixed(1)} °C / ${live.exhaust_rh != null ? live.exhaust_rh.toFixed(0) + '%' : '— RH'}`
            : '—';
        setText('ch-exhaust', exh);
        setText('ch-heater', live.heater_on ? 'ON' : 'OFF');
        const heater = comp.heater ?? {};
        setText('ch-heater-last', formatOutputLast(heater.last_cmd, heater.last_cmd_at));
        setText('ch-fans', live.fans_on ? `ON (${live.fan_reason ?? 'forced'})` : 'OFF');
        const fans = comp.fans ?? {};
        setText('ch-fans-last', formatOutputLast(fans.last_cmd, fans.last_cmd_at));

        // --- 5. Camera ---
        const cam = comp.camera ?? {};
        const camOk = cam.status === 'ok' || cam.ok === true;
        setBadge('cam-state', camOk);
        setText('cam-last', cam.last_capture_at ? new Date(cam.last_capture_at).toLocaleTimeString() : '—');
        setText('cam-model-state', cam.model_ready ? 'TFLite Model Active' : 'Fallback / Mock');

        if (cam.last_test) {
            const t = cam.last_test;
            const gradeEl = document.getElementById('testResultGrade');
            if (gradeEl) {
                gradeEl.textContent = t.grade.toUpperCase();
                gradeEl.className = `fs-4 fw-bold badge mt-1 ${t.grade === 'great' ? 'bg-success' : (t.grade === 'good' ? 'bg-info text-dark' : 'bg-danger')}`;
            }
            setText('testResultConfidence', (t.confidence * 100).toFixed(1) + '%');
            setText('testResultBlur', t.blur_laplacian);
            setText('testResultLatency', t.latency_ms);
            setText('cam-latency', t.latency_ms);

            const blurStatus = document.getElementById('testResultBlurStatus');
            if (blurStatus) {
                const pass = t.blur_laplacian >= t.blur_threshold;
                blurStatus.textContent = pass ? `✓ Sharp (>= ${t.blur_threshold})` : `✗ Blurry (< ${t.blur_threshold})`;
                blurStatus.className = `small fw-bold ${pass ? 'text-success' : 'text-danger'}`;
            }

            const scores = t.scores || {};
            const pGreat = ((scores.great || 0) * 100).toFixed(1);
            const pGood = ((scores.good || 0) * 100).toFixed(1);
            const pBad = ((scores.bad || 0) * 100).toFixed(1);

            const bGreat = document.getElementById('barProbGreat');
            const bGood = document.getElementById('barProbGood');
            const bBad = document.getElementById('barProbBad');
            if (bGreat) { bGreat.style.width = pGreat + '%'; bGreat.textContent = `Great: ${pGreat}%`; }
            if (bGood) { bGood.style.width = pGood + '%'; bGood.textContent = `Good: ${pGood}%`; }
            if (bBad) { bBad.style.width = pBad + '%'; bBad.textContent = `Bad: ${pBad}%`; }

            setText('probValGreat', pGreat + '%');
            setText('probValGood', pGood + '%');
            setText('probValBad', pBad + '%');
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (window.CORDS && typeof window.CORDS.startLivePolling === 'function') {
            window.CORDS.startLivePolling(render);
        }
    });

    // Calibration Lock Toggle
    async function toggleLock() {
        const banner = document.getElementById('calLockBanner');
        const isLocked = banner && !banner.classList.contains('d-none');
        const cmd = isLocked ? 'release_calibration_lock' : 'acquire_calibration_lock';
        const payload = isLocked ? {} : { duration_s: 600, component: 'conveyor' };
        try {
            await window.CORDS.sendCommand(cmd, payload);
        } catch (err) {
            alert('Lock request failed: ' + err.message);
        }
    }

    // Update Wizard UI & Calculations
    function updateWizardUI() {
        const leg = wizardState.currentLeg;
        const trials = wizardState.trials[leg];
        const tbody = document.getElementById('trialTableBody');
        setText('trialCount', trials.length);

        if (tbody) {
            if (trials.length === 0) {
                tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No trials recorded yet</td></tr>';
            } else {
                tbody.innerHTML = trials.map((val, idx) => `
                    <tr>
                        <td>Trial ${idx + 1}</td>
                        <td class="fw-bold">${val}</td>
                        <td><button class="btn btn-xs btn-outline-danger py-0 px-1 delete-trial" data-idx="${idx}">&times;</button></td>
                    </tr>
                `).join('');
                tbody.querySelectorAll('.delete-trial').forEach(b => {
                    b.addEventListener('click', (e) => {
                        const idx = parseInt(e.target.dataset.idx, 10);
                        trials.splice(idx, 1);
                        updateWizardUI();
                    });
                });
            }
        }

        const btnSave = document.getElementById('btnWizardSaveLeg');
        const badge = document.getElementById('wizardStatusBadge');

        if (trials.length === 0) {
            setText('statMean', '—');
            setText('statMin', '—');
            setText('statMax', '—');
            setText('statSpread', '—%');
            if (btnSave) btnSave.disabled = true;
            if (badge) {
                badge.textContent = 'Step 1: Set Reference';
                badge.className = 'badge bg-secondary';
            }
            return;
        }

        const sum = trials.reduce((a, b) => a + b, 0);
        const mean = sum / trials.length;
        const min = Math.min(...trials);
        const max = Math.max(...trials);
        const spread = mean > 0 ? (((max - min) / mean) * 100) : 0;

        setText('statMean', Math.round(mean));
        setText('statMin', min);
        setText('statMax', max);
        setText('statSpread', spread.toFixed(1) + '%');

        const valid = trials.length >= 3 && spread <= 2.0;
        if (btnSave) btnSave.disabled = !valid;

        if (badge) {
            if (trials.length < 3) {
                badge.textContent = `Need ${3 - trials.length} more trial(s)`;
                badge.className = 'badge bg-info text-dark';
            } else if (spread > 2.0) {
                badge.textContent = 'High Spread (> 2%) - Recheck';
                badge.className = 'badge bg-warning text-dark';
            } else {
                badge.textContent = 'Ready to Save (Spread <= 2%)';
                badge.className = 'badge bg-success';
            }
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        const api = window.CORDS;
        if (!api || typeof api.sendCommand !== 'function') return;

        const bindCommand = (id, command, payload) => {
            const button = document.getElementById(id);
            if (button) button.addEventListener('click', () => api.sendCommand(command, payload));
        };

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

        const btnLock = document.getElementById('btnLockHopper');
        if (btnLock) btnLock.addEventListener('click', toggleLock);

        // --- Hopper Actions ---
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

        // --- Carriage Actions ---
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

        // --- Conveyor Jog Controller ---
        document.querySelectorAll('[data-jog]').forEach(btn => {
            btn.addEventListener('click', () => {
                const steps = parseInt(btn.dataset.jog, 10);
                api.sendCommand('jog_conveyor', { steps: Math.abs(steps), forward: steps > 0 });
            });
        });

        const inputCustomJog = document.getElementById('inputCustomJog');
        const btnCustomJogFwd = document.getElementById('btnCustomJogFwd');
        const btnCustomJogRev = document.getElementById('btnCustomJogRev');
        if (btnCustomJogFwd && inputCustomJog) {
            btnCustomJogFwd.addEventListener('click', () => {
                const s = Math.abs(parseInt(inputCustomJog.value, 10) || 500);
                api.sendCommand('jog_conveyor', { steps: s, forward: true });
            });
        }
        if (btnCustomJogRev && inputCustomJog) {
            btnCustomJogRev.addEventListener('click', () => {
                const s = Math.abs(parseInt(inputCustomJog.value, 10) || 500);
                api.sendCommand('jog_conveyor', { steps: s, forward: false });
            });
        }

        bindCommand('btnHoldJogFwd', 'jog_conveyor', { steps: 2000, forward: true });
        bindCommand('btnHoldJogRev', 'jog_conveyor', { steps: 2000, forward: false });
        bindCommand('btnStopConveyor', 'stop_conveyor', {});

        // Driver settings save
        const btnSaveDriver = document.getElementById('btnSaveDriverConfig');
        if (btnSaveDriver) {
            btnSaveDriver.addEventListener('click', () => {
                const p00 = parseInt(document.getElementById('inputP00')?.value, 10) || 10000;
                const hz = parseFloat(document.getElementById('inputConveyorHz')?.value) || 400.0;
                api.sendCommand('set_conveyor_p00', { p00, hz });
            });
        }

        const btnAckFresh = document.getElementById('btnAcknowledgeFresh');
        if (btnAckFresh) {
            btnAckFresh.addEventListener('click', () => {
                api.sendCommand('mark_conveyor_stale', { stale: false });
            });
        }

        // --- 3-Leg Calibration Wizard ---
        const legPills = ['pillLeg1', 'pillLeg2', 'pillLeg3'];
        const legInstructions = {
            hopper_to_chamber: '<strong>Protocol for Leg 1 (Hopper &rarr; Chamber):</strong><br>1. Place marker under hopper drop chute.<br>2. Click <strong>"Set Start (Zero Ref)"</strong>.<br>3. Jog forward until marker enters chamber center.<br>4. Record trial (&ge; 3 trials, spread &le; 2%).',
            chamber_to_camera: '<strong>Protocol for Leg 2 (Chamber &rarr; Camera):</strong><br>1. Place marker at chamber center point.<br>2. Click <strong>"Set Start (Zero Ref)"</strong>.<br>3. Jog forward until marker is centered in camera view.<br>4. Record trial (&ge; 3 trials, spread &le; 2%).',
            camera_to_discharge: '<strong>Protocol for Leg 3 (Camera &rarr; Discharge):</strong><br>1. Place marker at camera view center.<br>2. Click <strong>"Set Start (Zero Ref)"</strong>.<br>3. Jog forward until marker drops off discharge chute roller.<br>4. Record trial (&ge; 3 trials, spread &le; 2%).'
        };

        legPills.forEach(pId => {
            const pill = document.getElementById(pId);
            if (!pill) return;
            pill.addEventListener('click', () => {
                legPills.forEach(id => document.getElementById(id)?.classList.remove('active'));
                pill.classList.add('active');
                wizardState.currentLeg = pill.dataset.leg;
                wizardState.baselinePos = null;
                setText('wizardDeltaSteps', '0');
                setText('wizardBaselinePos', '—');
                const alertEl = document.getElementById('legInstructionAlert');
                if (alertEl) alertEl.innerHTML = legInstructions[wizardState.currentLeg];
                updateWizardUI();
            });
        });

        const btnSetStart = document.getElementById('btnWizardSetStart');
        if (btnSetStart) {
            btnSetStart.addEventListener('click', () => {
                wizardState.baselinePos = wizardState.currentPos;
                setText('wizardBaselinePos', wizardState.baselinePos);
                setText('wizardDeltaSteps', '0');
            });
        }

        const btnRecord = document.getElementById('btnWizardRecordTrial');
        if (btnRecord) {
            btnRecord.addEventListener('click', () => {
                if (wizardState.baselinePos === null) {
                    alert('Please click "Set Start (Zero Ref)" before recording a trial.');
                    return;
                }
                const delta = Math.abs(wizardState.currentPos - wizardState.baselinePos);
                if (delta === 0) {
                    alert('Conveyor has not moved from the start reference. Jog the conveyor first!');
                    return;
                }
                wizardState.trials[wizardState.currentLeg].push(delta);
                updateWizardUI();
            });
        }

        const btnSlip = document.getElementById('btnWizardSlipCheck');
        if (btnSlip) {
            btnSlip.addEventListener('click', () => {
                if (wizardState.baselinePos === null) {
                    alert('No active start reference set.');
                    return;
                }
                const delta = wizardState.currentPos - wizardState.baselinePos;
                if (delta <= 0) {
                    alert('Conveyor is already at or behind start position.');
                    return;
                }
                api.sendCommand('jog_conveyor', { steps: delta, forward: false });
            });
        }

        const btnClear = document.getElementById('btnWizardClearTrials');
        if (btnClear) {
            btnClear.addEventListener('click', () => {
                wizardState.trials[wizardState.currentLeg] = [];
                updateWizardUI();
            });
        }

        const btnSaveLeg = document.getElementById('btnWizardSaveLeg');
        if (btnSaveLeg) {
            btnSaveLeg.addEventListener('click', async () => {
                const trials = wizardState.trials[wizardState.currentLeg];
                if (trials.length < 3) return;
                const sum = trials.reduce((a, b) => a + b, 0);
                const mean = Math.round(sum / trials.length);
                const min = Math.min(...trials);
                const max = Math.max(...trials);
                const spread = parseFloat((((max - min) / mean) * 100).toFixed(2));

                btnSaveLeg.disabled = true;
                try {
                    await api.sendCommand('save_conveyor_leg', {
                        leg: wizardState.currentLeg,
                        steps: mean,
                        meta: {
                            trials,
                            mean,
                            min,
                            max,
                            spread_pct: spread,
                            saved_at: new Date().toISOString()
                        }
                    });
                    alert(`Saved ${wizardState.currentLeg}: ${mean} steps!`);
                } catch (err) {
                    alert('Save failed: ' + err.message);
                } finally {
                    btnSaveLeg.disabled = false;
                }
            });
        }

        const btnTestMove = document.getElementById('btnWizardTestMove');
        if (btnTestMove) {
            btnTestMove.addEventListener('click', () => {
                const statMean = document.getElementById('statMean')?.textContent;
                const steps = parseInt(statMean, 10);
                if (!steps || isNaN(steps)) {
                    alert('No valid trial mean available. Record trials first!');
                    return;
                }
                api.sendCommand('jog_conveyor', { steps, forward: true });
            });
        }

        // --- Camera Live Preview & Alignment ---
        const btnToggleCrosshair = document.getElementById('btnToggleCrosshair');
        const crosshairOverlay = document.getElementById('crosshairOverlay');
        if (btnToggleCrosshair && crosshairOverlay) {
            btnToggleCrosshair.addEventListener('click', () => {
                crosshairOverlay.classList.toggle('d-none');
            });
        }

        let previewInterval = null;
        const btnTogglePreview = document.getElementById('btnTogglePreview');
        const cameraPreviewImg = document.getElementById('cameraPreviewImg');
        const previewFpsStatus = document.getElementById('previewFpsStatus');

        function refreshPreview() {
            if (cameraPreviewImg) {
                cameraPreviewImg.src = `/api/camera/preview?t=${Date.now()}`;
            }
        }

        if (btnTogglePreview) {
            btnTogglePreview.addEventListener('click', () => {
                if (previewInterval) {
                    clearInterval(previewInterval);
                    previewInterval = null;
                    btnTogglePreview.innerHTML = '<i class="bi bi-play-circle"></i> Start Live Preview';
                    btnTogglePreview.className = 'btn btn-outline-primary';
                    if (previewFpsStatus) previewFpsStatus.textContent = 'Preview Paused';
                } else {
                    refreshPreview();
                    previewInterval = setInterval(refreshPreview, 1000);
                    btnTogglePreview.innerHTML = '<i class="bi bi-pause-circle"></i> Pause Live Preview';
                    btnTogglePreview.className = 'btn btn-primary';
                    if (previewFpsStatus) previewFpsStatus.textContent = 'Live (1.0 FPS)';
                }
            });
        }

        // --- Test Vision Pipeline ---
        const btnTestVision = document.getElementById('btnTestVision');
        if (btnTestVision) {
            btnTestVision.addEventListener('click', async () => {
                btnTestVision.disabled = true;
                btnTestVision.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Testing...';
                try {
                    const res = await api.sendCommand('test_component', { component: 'camera' });
                    setTimeout(refreshPreview, 500); // refresh the image with the tested frame
                    if (res && res.result) alert(res.result);
                } catch (err) {
                    alert('Camera test failed: ' + err.message);
                } finally {
                    btnTestVision.disabled = false;
                    btnTestVision.innerHTML = '<i class="bi bi-cpu"></i> Test Vision Pipeline';
                }
            });
        }
    });
})();