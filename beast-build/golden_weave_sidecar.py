#!/usr/bin/env python3
"""
golden_weave_sidecar.py — Read-only telemetry subscriber + inject_density experiment runner

Connects to Khra'gixx v5 daemon:
  5556 SUB  — telemetry JSON (every 10 cycles)
  5557 PUB  — commands (inject_density ONLY)
  5558 SUB  — density snapshots (raw float32)
  5559 SUB  — command ACKs
  5560 SUB  — stress field snapshots (sxx/syy/sxy packed float32)

Does NOT modify the observer or any existing system behavior.
All experiment data logged to golden-weave-experiments/

Usage:
  python golden_weave_sidecar.py                # monitor mode (read-only)
  python golden_weave_sidecar.py --experiment   # run inject_density experiment
"""

import zmq
import json
import time
import os
import sys
import struct
import numpy as np
from datetime import datetime
from collections import deque

# ── CONFIG ──────────────────────────────────────────────────────────────

DAEMON_HOST = "127.0.0.1"
TELEMETRY_PORT = 5556
COMMAND_PORT = 5557
SNAPSHOT_PORT = 5558
ACK_PORT = 5559
STRESS_PORT = 5560

NX, NY, Q = 1024, 1024, 9

EXPERIMENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "golden-weave-experiments")

# Hysteresis buffer config
HYSTERESIS_WINDOW = 50  # number of telemetry frames to track


# ── HYSTERESIS BUFFER ───────────────────────────────────────────────────

class HysteresisBuffer:
    """Ring buffer tracking recent telemetry for basin depth measurement."""

    def __init__(self, window=HYSTERESIS_WINDOW):
        self.window = window
        self.coherence = deque(maxlen=window)
        self.asymmetry = deque(maxlen=window)
        self.stress_xx = deque(maxlen=window)
        self.stress_yy = deque(maxlen=window)
        self.stress_xy = deque(maxlen=window)
        self.vorticity = deque(maxlen=window)
        self.vel_mean = deque(maxlen=window)
        self.cycles = deque(maxlen=window)

    def push(self, telem):
        """Push a telemetry frame into the buffer."""
        self.coherence.append(telem.get("coherence", 0.0))
        self.asymmetry.append(telem.get("asymmetry", 0.0))
        self.stress_xx.append(telem.get("stress_xx", 0.0))
        self.stress_yy.append(telem.get("stress_yy", 0.0))
        self.stress_xy.append(telem.get("stress_xy", 0.0))
        self.vorticity.append(telem.get("vorticity_mean", 0.0))
        self.vel_mean.append(telem.get("vel_mean", 0.0))
        self.cycles.append(telem.get("cycle", 0))

    @property
    def full(self):
        return len(self.coherence) >= self.window

    def baseline(self):
        """Return mean values as the pre-perturbation baseline."""
        if not self.coherence:
            return {}
        return {
            "coherence": np.mean(self.coherence),
            "asymmetry": np.mean(self.asymmetry),
            "stress_xx": np.mean(self.stress_xx),
            "stress_yy": np.mean(self.stress_yy),
            "stress_xy": np.mean(self.stress_xy),
            "vorticity": np.mean(self.vorticity),
            "vel_mean": np.mean(self.vel_mean),
        }

    def variance(self):
        """Return variance of tracked quantities (stability measure)."""
        if len(self.coherence) < 2:
            return {}
        return {
            "coherence_var": np.var(self.coherence),
            "asymmetry_var": np.var(self.asymmetry),
            "stress_xx_var": np.var(self.stress_xx),
            "vorticity_var": np.var(self.vorticity),
        }


# ── ZMQ CONNECTIONS ────────────────────────────────────────────────────

def create_sockets():
    """Create all ZMQ sockets. Returns (ctx, telemetry_sub, cmd_pub, snap_sub, ack_sub, stress_sub)."""
    ctx = zmq.Context()

    # Telemetry subscriber
    telem_sub = ctx.socket(zmq.SUB)
    telem_sub.setsockopt(zmq.SUBSCRIBE, b"")
    telem_sub.setsockopt(zmq.RCVHWM, 1)
    telem_sub.setsockopt(zmq.LINGER, 0)
    telem_sub.connect(f"tcp://{DAEMON_HOST}:{TELEMETRY_PORT}")

    # Command publisher (inject_density only)
    cmd_pub = ctx.socket(zmq.PUB)
    cmd_pub.setsockopt(zmq.SNDHWM, 10)
    cmd_pub.setsockopt(zmq.LINGER, 0)
    cmd_pub.connect(f"tcp://{DAEMON_HOST}:{COMMAND_PORT}")

    # Density snapshot subscriber
    snap_sub = ctx.socket(zmq.SUB)
    snap_sub.setsockopt(zmq.SUBSCRIBE, b"")
    snap_sub.setsockopt(zmq.RCVHWM, 1)
    snap_sub.setsockopt(zmq.LINGER, 0)
    snap_sub.connect(f"tcp://{DAEMON_HOST}:{SNAPSHOT_PORT}")

    # ACK subscriber
    ack_sub = ctx.socket(zmq.SUB)
    ack_sub.setsockopt(zmq.SUBSCRIBE, b"")
    ack_sub.setsockopt(zmq.RCVHWM, 10)
    ack_sub.setsockopt(zmq.LINGER, 0)
    ack_sub.connect(f"tcp://{DAEMON_HOST}:{ACK_PORT}")

    # Stress field snapshot subscriber
    stress_sub = ctx.socket(zmq.SUB)
    stress_sub.setsockopt(zmq.SUBSCRIBE, b"")
    stress_sub.setsockopt(zmq.RCVHWM, 1)
    stress_sub.setsockopt(zmq.LINGER, 0)
    stress_sub.connect(f"tcp://{DAEMON_HOST}:{STRESS_PORT}")

    return ctx, telem_sub, cmd_pub, snap_sub, ack_sub, stress_sub


# ── SNAPSHOT DECODERS ──────────────────────────────────────────────────

def decode_density_snapshot(data):
    """Decode 8-byte header + float32 rho array from port 5558."""
    if len(data) < 8:
        return None, None
    cycle, w, h = struct.unpack_from("<IHH", data, 0)
    expected = 8 + w * h * 4
    if len(data) != expected:
        print(f"[SIDECAR] Density snapshot size mismatch: got {len(data)}, expected {expected}")
        return None, None
    rho = np.frombuffer(data, dtype=np.float32, offset=8).reshape(h, w)
    return cycle, rho


def decode_stress_snapshot(data):
    """Decode 8-byte header + 3×float32 field arrays from port 5560.
    Returns (cycle, sxx, syy, sxy) as NX×NY arrays."""
    if len(data) < 8:
        return None, None, None, None
    cycle, w, h = struct.unpack_from("<IHH", data, 0)
    field_size = w * h * 4
    expected = 8 + 3 * field_size
    if len(data) != expected:
        print(f"[SIDECAR] Stress snapshot size mismatch: got {len(data)}, expected {expected}")
        return None, None, None, None
    sxx = np.frombuffer(data, dtype=np.float32, offset=8, count=w*h).reshape(h, w)
    syy = np.frombuffer(data, dtype=np.float32, offset=8+field_size, count=w*h).reshape(h, w)
    sxy = np.frombuffer(data, dtype=np.float32, offset=8+2*field_size, count=w*h).reshape(h, w)
    return cycle, sxx, syy, sxy


# ── COMMANDS ───────────────────────────────────────────────────────────

def send_inject_density(cmd_pub, x, y, sigma=16.0, strength=0.1):
    """Send inject_density command to v5 daemon."""
    msg = json.dumps({
        "cmd": "inject_density",
        "x": float(x), "y": float(y),
        "sigma": float(sigma), "strength": float(strength)
    })
    cmd_pub.send_string(msg)
    print(f"[SIDECAR → DAEMON] {msg}")
    sys.stdout.flush()


def send_stress_snapshot_now(cmd_pub):
    """Request a stress field snapshot from v5 daemon."""
    msg = json.dumps({"cmd": "stress_snapshot_now"})
    cmd_pub.send_string(msg)
    print(f"[SIDECAR → DAEMON] {msg}")
    sys.stdout.flush()


def send_snapshot_now(cmd_pub):
    """Request a density snapshot from v5 daemon."""
    msg = json.dumps({"cmd": "snapshot_now"})
    cmd_pub.send_string(msg)
    print(f"[SIDECAR → DAEMON] {msg}")
    sys.stdout.flush()


# ── EXPERIMENT FRAMEWORK ───────────────────────────────────────────────

def wait_for_ack(ack_sub, expected_cmd, timeout_ms=2000):
    """Wait for ACK from daemon. Returns ACK dict or None."""
    poller = zmq.Poller()
    poller.register(ack_sub, zmq.POLLIN)
    events = dict(poller.poll(timeout_ms))
    if ack_sub in events:
        raw = ack_sub.recv_string()
        try:
            ack = json.loads(raw)
            if ack.get("ack") == expected_cmd:
                return ack
        except json.JSONDecodeError:
            pass
    return None


def collect_telemetry(telem_sub, n_frames, timeout_per_frame_ms=500):
    """Collect n telemetry frames. Returns list of dicts."""
    frames = []
    poller = zmq.Poller()
    poller.register(telem_sub, zmq.POLLIN)
    for _ in range(n_frames):
        events = dict(poller.poll(timeout_per_frame_ms))
        if telem_sub in events:
            raw = telem_sub.recv_string()
            try:
                frames.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return frames


def run_injection_experiment(cmd_pub, telem_sub, ack_sub, snap_sub, stress_sub,
                             x, y, sigma=16.0, strength=0.1,
                             pre_frames=50, post_frames=100):
    """
    Run a single inject_density experiment:
    1. Collect pre_frames of baseline telemetry
    2. Request density + stress snapshots (pre)
    3. Fire inject_density
    4. Collect post_frames of recovery telemetry
    5. Request density + stress snapshots (post)
    6. Return experiment record
    """
    exp_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n{'='*60}")
    print(f"[EXPERIMENT {exp_id}] inject_density at ({x}, {y}) σ={sigma} str={strength}")
    print(f"{'='*60}")
    sys.stdout.flush()

    # Phase 1: Baseline
    print(f"[EXP] Collecting {pre_frames} baseline frames...")
    sys.stdout.flush()
    baseline_frames = collect_telemetry(telem_sub, pre_frames)
    if not baseline_frames:
        print("[EXP] ERROR: No telemetry received during baseline")
        return None

    buf = HysteresisBuffer(window=len(baseline_frames))
    for f in baseline_frames:
        buf.push(f)
    baseline = buf.baseline()
    baseline_var = buf.variance()
    print(f"[EXP] Baseline: coh={baseline.get('coherence',0):.4f} "
          f"asym={baseline.get('asymmetry',0):.4f} "
          f"vort={baseline.get('vorticity',0):.6f}")
    sys.stdout.flush()

    # Phase 2: Pre-injection snapshots
    send_snapshot_now(cmd_pub)
    send_stress_snapshot_now(cmd_pub)

    pre_rho = None
    pre_stress = None
    # Poll for snapshots with proper timeout instead of sleep+NOBLOCK
    snap_poller = zmq.Poller()
    snap_poller.register(snap_sub, zmq.POLLIN)
    snap_poller.register(stress_sub, zmq.POLLIN)
    deadline = time.time() + 2.0  # 2s total budget for both snapshots
    got_rho, got_stress = False, False
    while time.time() < deadline and not (got_rho and got_stress):
        remaining_ms = max(1, int((deadline - time.time()) * 1000))
        events = dict(snap_poller.poll(remaining_ms))
        if snap_sub in events and not got_rho:
            raw = snap_sub.recv()
            _, pre_rho = decode_density_snapshot(raw)
            got_rho = True
        if stress_sub in events and not got_stress:
            raw = stress_sub.recv()
            _, pre_sxx, pre_syy, pre_sxy = decode_stress_snapshot(raw)
            pre_stress = (pre_sxx, pre_syy, pre_sxy)
            got_stress = True
    if not got_rho:
        print("[EXP] WARNING: Pre-inject density snapshot not received")
    if not got_stress:
        print("[EXP] WARNING: Pre-inject stress snapshot not received")
    sys.stdout.flush()

    # Phase 3: Inject
    inject_cycle = baseline_frames[-1].get("cycle", 0) if baseline_frames else 0
    send_inject_density(cmd_pub, x, y, sigma, strength)
    ack = wait_for_ack(ack_sub, "inject_density", timeout_ms=2000)
    if ack:
        print(f"[EXP] ACK received: {ack}")
    else:
        print("[EXP] WARNING: No ACK for inject_density (may still have worked)")
    sys.stdout.flush()

    # Phase 4: Recovery
    print(f"[EXP] Collecting {post_frames} recovery frames...")
    sys.stdout.flush()
    recovery_frames = collect_telemetry(telem_sub, post_frames)

    # Phase 5: Post-injection snapshots
    send_snapshot_now(cmd_pub)
    send_stress_snapshot_now(cmd_pub)

    post_rho = None
    post_stress = None
    # Poll for snapshots with proper timeout
    deadline = time.time() + 2.0
    got_rho, got_stress = False, False
    while time.time() < deadline and not (got_rho and got_stress):
        remaining_ms = max(1, int((deadline - time.time()) * 1000))
        events = dict(snap_poller.poll(remaining_ms))
        if snap_sub in events and not got_rho:
            raw = snap_sub.recv()
            _, post_rho = decode_density_snapshot(raw)
            got_rho = True
        if stress_sub in events and not got_stress:
            raw = stress_sub.recv()
            _, post_sxx, post_syy, post_sxy = decode_stress_snapshot(raw)
            post_stress = (post_sxx, post_syy, post_sxy)
            got_stress = True
    if not got_rho:
        print("[EXP] WARNING: Post-inject density snapshot not received")
    if not got_stress:
        print("[EXP] WARNING: Post-inject stress snapshot not received")
    sys.stdout.flush()

    # Phase 6: Analyze
    record = {
        "experiment_id": exp_id,
        "inject_x": x, "inject_y": y,
        "inject_sigma": sigma, "inject_strength": strength,
        "inject_cycle": inject_cycle,
        "baseline": baseline,
        "baseline_variance": baseline_var,
        "baseline_frames": len(baseline_frames),
        "recovery_frames": len(recovery_frames),
    }

    if recovery_frames:
        # Measure deviation from baseline
        post_buf = HysteresisBuffer(window=len(recovery_frames))
        for f in recovery_frames:
            post_buf.push(f)
        post_mean = post_buf.baseline()
        record["post_mean"] = post_mean

        # Basin depth = max |deviation| during recovery
        max_coh_dev = 0.0
        max_asym_dev = 0.0
        recovery_cycles = []
        for f in recovery_frames:
            coh_dev = abs(f.get("coherence", 0) - baseline["coherence"])
            asym_dev = abs(f.get("asymmetry", 0) - baseline["asymmetry"])
            if coh_dev > max_coh_dev:
                max_coh_dev = coh_dev
            if asym_dev > max_asym_dev:
                max_asym_dev = asym_dev
            recovery_cycles.append(f.get("cycle", 0))

        record["max_coherence_deviation"] = max_coh_dev
        record["max_asymmetry_deviation"] = max_asym_dev

        # Recovery time: cycles until coherence returns within 1 baseline_var
        coh_var = baseline_var.get("coherence_var", 1e-6)
        threshold = max(np.sqrt(coh_var) * 2, 1e-4)
        recovery_cycle = None
        for f in recovery_frames:
            if abs(f.get("coherence", 0) - baseline["coherence"]) < threshold:
                recovery_cycle = f.get("cycle", 0)
                break
        if recovery_cycle is not None and inject_cycle > 0:
            record["recovery_cycles"] = recovery_cycle - inject_cycle
        else:
            record["recovery_cycles"] = None

        print(f"[EXP] Max deviation: coh={max_coh_dev:.6f} asym={max_asym_dev:.6f}")
        print(f"[EXP] Recovery: {'%d cycles' % record['recovery_cycles'] if record['recovery_cycles'] else 'not recovered'}")
    else:
        print("[EXP] WARNING: No recovery frames collected")

    sys.stdout.flush()

    # Save experiment
    os.makedirs(EXPERIMENT_DIR, exist_ok=True)
    exp_path = os.path.join(EXPERIMENT_DIR, f"exp_{exp_id}.json")
    # Convert numpy types for JSON serialization
    def sanitize(obj):
        if isinstance(obj, (np.floating, np.float32, np.float64)):
            return float(obj)
        if isinstance(obj, (np.integer, np.int32, np.int64)):
            return int(obj)
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [sanitize(v) for v in obj]
        return obj

    with open(exp_path, "w") as f:
        json.dump(sanitize(record), f, indent=2)
    print(f"[EXP] Saved: {exp_path}")

    # Save snapshots if available
    if pre_rho is not None:
        np.save(os.path.join(EXPERIMENT_DIR, f"exp_{exp_id}_pre_rho.npy"), pre_rho)
    if post_rho is not None:
        np.save(os.path.join(EXPERIMENT_DIR, f"exp_{exp_id}_post_rho.npy"), post_rho)
    if pre_stress is not None:
        for name, arr in zip(["sxx", "syy", "sxy"], pre_stress):
            np.save(os.path.join(EXPERIMENT_DIR, f"exp_{exp_id}_pre_{name}.npy"), arr)
    if post_stress is not None:
        for name, arr in zip(["sxx", "syy", "sxy"], post_stress):
            np.save(os.path.join(EXPERIMENT_DIR, f"exp_{exp_id}_post_{name}.npy"), arr)

    sys.stdout.flush()
    return record


# ── MONITOR MODE ───────────────────────────────────────────────────────

def monitor_loop(telem_sub, snap_sub, stress_sub):
    """Read-only monitor: print telemetry, decode snapshots when they arrive."""
    poller = zmq.Poller()
    poller.register(telem_sub, zmq.POLLIN)
    poller.register(snap_sub, zmq.POLLIN)
    poller.register(stress_sub, zmq.POLLIN)

    buf = HysteresisBuffer()
    frame_count = 0

    print("[SIDECAR] Monitor mode — Ctrl+C to exit")
    sys.stdout.flush()

    while True:
        events = dict(poller.poll(1000))

        if telem_sub in events:
            raw = telem_sub.recv_string()
            try:
                telem = json.loads(raw)
                buf.push(telem)
                frame_count += 1
                if frame_count % 10 == 0:
                    cycle = telem.get("cycle", "?")
                    coh = telem.get("coherence", 0)
                    asym = telem.get("asymmetry", 0)
                    vort = telem.get("vorticity_mean", 0)
                    print(f"[TELEM] cycle={cycle} coh={coh:.4f} asym={asym:.4f} vort={vort:.6f}")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                pass

        if snap_sub in events:
            data = snap_sub.recv()
            cycle, rho = decode_density_snapshot(data)
            if rho is not None:
                print(f"[SNAP] Density snapshot: cycle={cycle} "
                      f"rho_mean={rho.mean():.4f} rho_std={rho.std():.6f}")
                sys.stdout.flush()

        if stress_sub in events:
            data = stress_sub.recv()
            cycle, sxx, syy, sxy = decode_stress_snapshot(data)
            if sxx is not None:
                print(f"[STRESS] Stress snapshot: cycle={cycle} "
                      f"sxx_mean={sxx.mean():.6f} syy_mean={syy.mean():.6f} "
                      f"sxy_mean={sxy.mean():.6f}")
                sys.stdout.flush()


# ── MAIN ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Golden-Weave Sidecar for Khra'gixx v5")
    parser.add_argument("--experiment", action="store_true",
                        help="Run inject_density experiment instead of monitor mode")
    parser.add_argument("--x", type=float, default=512.0,
                        help="Injection center X (default: 512)")
    parser.add_argument("--y", type=float, default=512.0,
                        help="Injection center Y (default: 512)")
    parser.add_argument("--sigma", type=float, default=16.0,
                        help="Gaussian width (default: 16)")
    parser.add_argument("--strength", type=float, default=0.1,
                        help="Injection strength (default: 0.1)")
    parser.add_argument("--pre-frames", type=int, default=50,
                        help="Baseline telemetry frames to collect (default: 50)")
    parser.add_argument("--post-frames", type=int, default=100,
                        help="Recovery telemetry frames to collect (default: 100)")
    args = parser.parse_args()

    print("=" * 60)
    print("GOLDEN-WEAVE SIDECAR — Khra'gixx v5 Integration")
    print("=" * 60)
    print(f"Telemetry: tcp://{DAEMON_HOST}:{TELEMETRY_PORT}")
    print(f"Commands:  tcp://{DAEMON_HOST}:{COMMAND_PORT}")
    print(f"Snapshots: tcp://{DAEMON_HOST}:{SNAPSHOT_PORT}")
    print(f"ACK:       tcp://{DAEMON_HOST}:{ACK_PORT}")
    print(f"Stress:    tcp://{DAEMON_HOST}:{STRESS_PORT}")
    sys.stdout.flush()

    ctx, telem_sub, cmd_pub, snap_sub, ack_sub, stress_sub = create_sockets()

    # Brief pause for ZMQ connections to establish
    time.sleep(0.5)

    try:
        if args.experiment:
            os.makedirs(EXPERIMENT_DIR, exist_ok=True)
            result = run_injection_experiment(
                cmd_pub, telem_sub, ack_sub, snap_sub, stress_sub,
                x=args.x, y=args.y, sigma=args.sigma, strength=args.strength,
                pre_frames=args.pre_frames, post_frames=args.post_frames,
            )
            if result:
                print(f"\n[SIDECAR] Experiment complete. Results in {EXPERIMENT_DIR}/")
            else:
                print("\n[SIDECAR] Experiment failed — check daemon is running")
        else:
            monitor_loop(telem_sub, snap_sub, stress_sub)
    except KeyboardInterrupt:
        print("\n[SIDECAR] Shutting down...")
    finally:
        telem_sub.close()
        cmd_pub.close()
        snap_sub.close()
        ack_sub.close()
        stress_sub.close()
        ctx.term()
        print("[SIDECAR] Clean exit")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
