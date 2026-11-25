"""Analyze INET handovers and replay Survey observations through a roaming policy."""

import argparse
import math
import sys
from bisect import bisect_left
from collections import deque
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from result_io import estimate, load_params, read_runs, write_table


APS = ("ap1", "ap2")
AGENT = "Roaming.host.wlan.agent"
RADIO = "Roaming.host.wlan.radio"
DELIVERED = "packets:vector(packetLength)"
CHANNEL = "radioChannel:vector"
SNIR = "minSnir:vector"
REQUESTS = "sentRequest:vector"
CONFIRMS = "acceptConfirm:vector"
REJECTIONS = "dropConfirm:vector"
SCAN, AUTH, ASSOC = 1, 2, 4
PROBE_S, MIN_S, MAX_S = 0.100, 0.150, 0.300
SENSITIVITY_DBM = -85
SIM_END_S = 120
FIRST_CONFIGS = ("Walk", "ScanNeighbor")
WALK_VECTORS = {(f"Roaming.{ap}.sink", DELIVERED) for ap in APS} | {
    (AGENT, REQUESTS), (AGENT, CONFIRMS), (AGENT, REJECTIONS), (RADIO, CHANNEL)}
SURVEY_VECTORS = {(f"Roaming.meter.wlan[{i}].radio", SNIR) for i in range(2)}
HYSTERESIS = (0, 2, 4, 6, 8, 10)
WINDOWS = (1, 10)


def position(time, params):
    return min(time * params["v"], params["D"])


def nominal_scan(channels, active):
    return channels * (PROBE_S + MIN_S) + active * (MAX_S - MIN_S)


def prediction_rows(params):
    for config, channels in (("Walk", params["nCh"]), ("ScanNeighbor", 2)):
        yield {"metric": f"{config} scan duration if both AP channels are active",
               "value": nominal_scan(channels, 2), "unit": "s"}
    yield {"metric": "Beacon timeout", "value": 3.5 * params["BI"] / 1000, "unit": "s"}
    yield {"metric": "Equal mean power location", "value": params["D"] / 2, "unit": "m"}
    yield {"metric": "6 dB noiseless hysteresis crossing, ignoring 2 m offset and threshold",
           "value": params["D"] / (1 + 10 ** (-6 / (10 * params["alpha"]))), "unit": "m"}


def handover(run, config, params):
    """Locate the first AP1-to-AP2 voice delivery transition."""
    times = [[t for t, _ in run.vectors.get((f"Roaming.{ap}.sink", DELIVERED), [])] for ap in APS]
    scans = [t for t, code in run.vectors.get((AGENT, REQUESTS), []) if code == SCAN]
    associations = [t for t, code in run.vectors.get((AGENT, CONFIRMS), []) if code == ASSOC]
    summary = {"config": config, "repetition": run.repetition}
    if not times[0]:
        return {**summary, "status": "no AP1 deliveries"}, []
    first_ap2 = next((t for t in times[1] if t > times[0][0]), None)
    if first_ap2 is None:
        return {**summary, "status": "no AP2 deliveries after AP1"}, []
    last_ap1 = times[0][bisect_left(times[0], first_ap2) - 1]
    last_assoc = max((t for t in associations if t <= last_ap1), default=-1)
    relevant_scans = [t for t in scans if last_assoc < t <= first_ap2]
    if not relevant_scans:
        return {**summary, "status": "missing handover scan events",
                "last_ap1_s": last_ap1, "first_ap2_s": first_ap2,
                "outage_s": first_ap2 - last_ap1}, []
    scan_start = relevant_scans[0]
    scan_position = position(scan_start, params)
    midpoint = params["D"] / 2
    events = [
        (last_ap1, 0, "Last AP1 voice frame", "Associated"),
        (first_ap2, 9, "First AP2 voice frame", "Associated"),
    ]
    codes = {SCAN: ("Scan", "Scanning"), AUTH: ("Authentication", "Authenticating"),
             ASSOC: ("Association", "Associating")}
    scan_ends = []
    for signal, suffix in ((REQUESTS, "started"), (CONFIRMS, "confirmed"), (REJECTIONS, "rejected")):
        for time, code in run.vectors.get((AGENT, signal), []):
            if scan_start <= time <= first_ap2 and code in codes:
                name, state = codes[code]
                if signal != REQUESTS:
                    next_state = {SCAN: "Authenticating", AUTH: "Associating", ASSOC: "Associated"}[code]
                    state = f"{state} -> {next_state if signal == CONFIRMS else 'Scanning (retry)'}"
                order = 4 if signal == REQUESTS else 3
                if code == SCAN and signal == REQUESTS and time == scan_start:
                    state = "Associated -> Scanning"
                    order = 1
                events.append((time, order, f"{name} {suffix}", state))
                if code == SCAN and signal != REQUESTS:
                    scan_ends.append(time)

    radio = run.vectors.get((RADIO, CHANNEL), [])
    dwell = []
    first_channel = 1 if config == "ScanNeighbor" else 0
    scan_intervals = list(zip(relevant_scans, sorted(scan_ends)))
    for start, end in scan_intervals:
        changes = [(t, int(ch)) for t, ch in radio if start <= t < end]
        if not changes or changes[0][1] != first_channel:
            changes.insert(0, (start, first_channel))
        for (time, ch), (next_time, _) in zip(changes, changes[1:] + [(end, None)]):
            duration = next_time - time
            dwell.append(duration)
            events.append((time, 2, f"Channel {ch} dwell started ({duration:.6f} s)",
                           "Scanning -> Scanning"))
    timeline = [{"time_s": time, "position_m": position(time, params), "event": event, "state": state}
                for time, _, event, state in sorted(events)]
    return {**summary, "status": "ok", "last_ap1_s": last_ap1, "first_ap2_s": first_ap2,
            "outage_s": first_ap2 - last_ap1, "scan_start_s": scan_start,
            "scan_position_m": scan_position,
            "sticky_extra_m": scan_position - midpoint,
            "sticky_extra_s": scan_start - midpoint / params["v"],
            "scan_attempts": len(relevant_scans),
            "observed_scan_s": sum(end - start for start, end in scan_intervals),
            "long_channel_dwells": sum(t >= PROBE_S + (MIN_S + MAX_S) / 2 for t in dwell)}, timeline


def survey_samples(directory, params, noise_dbm):
    for run in read_runs(directory, "Survey", "vec", SURVEY_VECTORS):
        samples = []
        for i, ap in enumerate(APS):
            vector = run.vectors.get((f"Roaming.meter.wlan[{i}].radio", SNIR), [])
            if not vector:
                raise ValueError(f"{run.name}: no Survey SNIR samples for {ap}")
            for time, snir in vector:
                if snir > 0 and math.isfinite(snir):
                    x = position(time, params)
                    distance = math.hypot(x if i == 0 else params["D"] - x, 2)
                    samples.append({"repetition": run.repetition, "time_s": time, "ap": ap,
                                    "position_m": x, "distance_m": distance,
                                    "power_dbm": 10 * math.log10(snir) + noise_dbm})
        yield run.repetition, sorted(samples, key=lambda row: (row["time_s"], row["ap"]))


def replay(samples, params, threshold, hysteresis, window):
    """States are disconnected, associated with AP1, and associated with AP2."""
    timeout = 3.5 * params["BI"] / 1000
    recent = {ap: deque(maxlen=window) for ap in APS}
    last_seen = {}
    current = None
    last_ap = None
    events = []

    def transition(time, target, reason):
        nonlocal current, last_ap
        handover = target is not None and last_ap is not None and target != last_ap
        events.append({"time_s": time, "position_m": position(time, params),
                       "from_ap": current, "to_ap": target, "reason": reason,
                       "handover": int(handover)})
        current = target
        if target is not None:
            last_ap = target

    def powers(time):
        return {ap: mean(recent[ap]) for ap in APS
                if ap in last_seen and time < last_seen[ap] + timeout}

    def expire_before(time):
        while current is not None:
            expiry = last_seen[current] + timeout
            if expiry > time:
                return
            transition(expiry, None, "beacon timeout")
            available = powers(expiry)
            if available:
                transition(expiry, max(available, key=available.get), "acquired after timeout")

    for sample in samples:
        time, ap = sample["time_s"], sample["ap"]
        expire_before(time)
        last_seen[ap] = time
        recent[ap].append(sample["power_dbm"])
        available = powers(time)
        if current is None:
            transition(time, max(available, key=available.get), "acquired beacon")
        elif current in available and len(available) == 2:
            other = APS[1] if current == APS[0] else APS[0]
            if available[current] < threshold and available[other] - available[current] >= hysteresis:
                transition(time, other, "below threshold and candidate meets hysteresis")
    expire_before(SIM_END_S)
    return events


def sweep(samples_by_run, params, threshold):
    rows, transitions = [], []
    for window in WINDOWS:
        for hysteresis in HYSTERESIS:
            for repetition, samples in samples_by_run:
                events = replay(samples, params, threshold, hysteresis, window)
                switches = [event for event in events if event["handover"]]
                rows.append({"repetition": repetition, "window": window,
                             "hysteresis_db": hysteresis, "threshold_dbm": threshold,
                             "handovers": len(switches),
                             "ping_pongs": max(0, len(switches) - 1),
                             "last_handover_m": switches[-1]["position_m"] if switches else None})
                transitions.extend({"repetition": repetition, "window": window,
                                    "hysteresis_db": hysteresis, **event} for event in events)
    return rows, transitions


def plot_results(output, params, samples, outage_stats, sweep_stats):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.8), layout="constrained")
    for ap in APS:
        points = [row for row in samples if row["ap"] == ap and row["time_s"] <= params["D"] / params["v"]]
        ax.scatter([row["distance_m"] for row in points], [row["power_dbm"] for row in points],
                   s=7, alpha=0.24, label=f"{ap} Survey")
    distances = [2 * (math.hypot(params["D"], 2) / 2) ** (i / 199) for i in range(200)]
    loss_at_one = 20 * math.log10(4 * math.pi * 5.03e9 / 299792458)
    ax.plot(distances, [10 * math.log10(20) - loss_at_one - 10 * params["alpha"] * math.log10(d)
                        for d in distances], color="black", label="Mean path-loss model")
    ax.axhline(SENSITIVITY_DBM, linestyle="--", color="gray", label="Receiver sensitivity")
    ax.set(xscale="log", xlabel="Distance to AP (m)", ylabel="Received power (dBm)")
    ax.legend()
    fig.savefig(output / "power.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4), layout="constrained")
    ax.bar([0, 1], [row["nominal_scan_s"] for row in outage_stats],
           width=0.38, align="edge", label="Nominal scan")
    for i, row in enumerate(outage_stats):
        if row["mean"] is not None:
            ax.bar(i - 0.38, row["mean"], width=0.38, align="edge", color="tab:orange",
                   yerr=row["ci95_half"], capsize=4, label="Measured outage" if i == 0 else None)
    ax.set_xticks([0, 1], FIRST_CONFIGS)
    ax.set_ylabel("Duration (s)")
    ax.legend()
    fig.savefig(output / "outage.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4), layout="constrained")
    for window in WINDOWS:
        rows = [row for row in sweep_stats if row["window"] == window]
        ax.plot([row["hysteresis_db"] for row in rows],
                [row["mean_handovers"] for row in rows], "o-", label=f"N = {window}")
    ax.set(xlabel="Hysteresis (dB)", ylabel="Mean number of handovers", xticks=HYSTERESIS)
    ax.legend()
    fig.savefig(output / "handover_counts.png", dpi=160)
    plt.close(fig)


def analyze(args, params):
    measurements, timeline = [], []
    for config in FIRST_CONFIGS:
        for run in read_runs(args.results, config, "vec", WALK_VECTORS):
            row, events = handover(run, config, params)
            measurements.append(row)
            timeline.extend({"config": config, "repetition": run.repetition, **event} for event in events)
    outage_stats = []
    for config, channels in (("Walk", params["nCh"]), ("ScanNeighbor", 2)):
        values = [row.get("outage_s") for row in measurements if row["config"] == config]
        outage_stats.append({"config": config, "runs": len(values),
                             "nominal_scan_s": nominal_scan(channels, 2), **estimate(values)})
    write_table(args.output / "walk_runs.csv",
                ("config", "repetition", "status", "last_ap1_s", "first_ap2_s", "outage_s",
                 "scan_start_s", "scan_position_m", "sticky_extra_m", "sticky_extra_s",
                 "scan_attempts", "observed_scan_s", "long_channel_dwells"), measurements)
    write_table(args.output / "timeline.csv", ("config", "repetition", "time_s", "position_m",
                "event", "state"), timeline)
    write_table(args.output / "outage.csv", outage_stats[0].keys(), outage_stats)

    by_run = list(survey_samples(args.results, params, args.noise_dbm))
    samples = [row for _, run in by_run for row in run]
    write_table(args.output / "survey_power.csv",
                ("repetition", "time_s", "ap", "position_m", "distance_m", "power_dbm"), samples)
    rows, transitions = sweep(by_run, params, args.threshold_dbm)
    write_table(args.output / "policy_runs.csv", rows[0].keys(), rows)
    write_table(args.output / "policy_transitions.csv",
                ("repetition", "window", "hysteresis_db", "time_s", "position_m",
                 "from_ap", "to_ap", "reason", "handover"), transitions)
    groups = []
    for window in WINDOWS:
        for hysteresis in HYSTERESIS:
            group = [row for row in rows if row["window"] == window and row["hysteresis_db"] == hysteresis]
            last_locations = [row["last_handover_m"] for row in group if row["last_handover_m"] is not None]
            groups.append({"window": window, "hysteresis_db": hysteresis, "runs": len(group),
                           "mean_handovers": mean(row["handovers"] for row in group),
                           "mean_ping_pongs": mean(row["ping_pongs"] for row in group),
                           "mean_last_handover_m": mean(last_locations) if last_locations else None,
                           "exactly_one_in_all_runs": int(all(row["handovers"] == 1 for row in group))})
    write_table(args.output / "policy_summary.csv", groups[0].keys(), groups)
    candidates = [row for row in groups if row["exactly_one_in_all_runs"] and row["runs"] >= 3]
    walk_positions = [row["scan_position_m"] for row in measurements
                      if row["config"] == "Walk" and "scan_position_m" in row]
    best = min(candidates, key=lambda row: row["mean_last_handover_m"], default=None)
    recommendation = {"status": "one switch in every Survey run" if best else "no qualifying combination",
                      "walk_mean_scan_m": mean(walk_positions) if walk_positions else None}
    if best:
        recommendation.update({key: best[key] for key in ("window", "hysteresis_db", "mean_last_handover_m")})
    write_table(args.output / "policy_choice.csv",
                ("status", "window", "hysteresis_db", "mean_last_handover_m", "walk_mean_scan_m"),
                [recommendation])
    plot_results(args.output, params, samples, outage_stats, groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=HERE / "results")
    parser.add_argument("--output", type=Path, default=HERE / "analysis")
    parser.add_argument("--noise-dbm", type=float, default=-110)
    parser.add_argument("--threshold-dbm", type=float, default=-75)
    parser.add_argument("--model-only", action="store_true")
    args = parser.parse_args()
    params = load_params(HERE.parent / "params.json")
    write_table(args.output / "predictions.csv", ("metric", "value", "unit"), prediction_rows(params))
    if not args.model_only:
        analyze(args, params)
    print(f"Phase 3 tables written to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(str(error))
