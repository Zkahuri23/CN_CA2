"""Model airtime fairness and compare it with the phase 2 scalar results."""

import argparse
import math
import sys
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from result_io import estimate, load_params, read_runs, write_table


CONFIGS = ("UplinkRuns", "DownlinkFifoRuns", "DownlinkAirtimeRuns",
           "DownlinkFifoLight", "DownlinkAirtimeLight")
MEASUREMENT_S = 10
QUANTUM_US = 500
SIFS_US = 16
DIFS_US = 34
BACKOFF_US = 15 / 2 * 9
HEADERS = {"UDP": 8, "IPv4": 20, "LLC/SNAP": 8, "802.11 MAC": 24, "FCS": 4}


def ofdm_duration_us(octets, rate_mbps):
    # 20 us for preamble and SIGNAL, then whole 4 us OFDM symbols.
    return 20 + 4 * math.ceil((16 + 8 * octets + 6) / (4 * rate_mbps))


def jain(values):
    square_sum = sum(value * value for value in values)
    return sum(values) ** 2 / (len(values) * square_sum) if square_sum else None


def model(params):
    payload = params["L"]
    rates = [params["Rslow"]] + [params["Rfast"]] * (params["n2"] - 1)
    mpdu = payload + sum(HEADERS.values())
    data = [ofdm_duration_us(mpdu, rate) for rate in rates]
    ack_rates = [max(basic for basic in (6, 12, 24) if basic <= rate) for rate in rates]
    ack = [ofdm_duration_us(14, rate) for rate in ack_rates]
    exchange = [d + SIFS_US + a for d, a in zip(data, ack)]
    cycle = [DIFS_US + BACKOFF_US + duration for duration in exchange]

    fifo_pps = 1e6 / sum(cycle)
    drr_pps = [1e6 / (d * sum(t / cost for t, cost in zip(cycle, data))) for d in data]
    no_slow_mbps = payload * 8 / sum(cycle[1:])
    stations = []
    for i, rate in enumerate(rates):
        stations.append({
            "station": f"host[{i}]", "data_mbps": rate, "ack_mbps": ack_rates[i],
            "mpdu_bytes": mpdu, "data_us": data[i], "ack_us": ack[i],
            "exchange_us": exchange[i], "cycle_us": cycle[i],
            "fifo_mbps": fifo_pps * payload * 8 / 1e6,
            "fifo_mbps_without_slow": no_slow_mbps if i else None,
            "fifo_fast_reduction_pct": 100 * (1 - fifo_pps * payload * 8 / 1e6 / no_slow_mbps) if i else None,
            "drr_mbps": drr_pps[i] * payload * 8 / 1e6,
            "fifo_airtime_fraction": fifo_pps * exchange[i] / 1e6,
            "drr_airtime_fraction": drr_pps[i] * exchange[i] / 1e6,
            "fifo_delay_ms": 1e3 * 100 / len(rates) / fifo_pps,
            "drr_delay_ms": 1e3 * 50 / drr_pps[i],
        })
    return stations, no_slow_mbps


def drr_trace(costs, decisions=10):
    """The first decisions for two continuously backlogged queues."""
    deficits = [0, 0]
    current = 0
    rows = []
    for step in range(1, decisions + 1):
        before = deficits.copy()
        while deficits[current] <= 0:
            deficits[current] += QUANTUM_US
            current = (current + 1) % len(costs)
        deficits[current] -= costs[current]
        rows.append({"decision": step, "queue": current, "slow_before_us": before[0],
                     "fast_before_us": before[1], "slow_after_us": deficits[0],
                     "fast_after_us": deficits[1]})
    return rows


def measure(config, directory, params, stations):
    rows = []
    for run in read_runs(directory, config, "sca"):
        per_station = []
        for i, model_station in enumerate(stations):
            module = (f"Anomaly.server.app[{i}]" if config == "UplinkRuns"
                      else f"Anomaly.host[{i}].app[0]")
            count = run.scalars.get((module, "packetReceived:count"))
            if count is None:
                raise ValueError(f"{run.name}: missing packetReceived:count for {module}")
            delay = run.statistics.get((module, "endToEndDelay:histogram"), {}).get("mean")
            per_station.append({
                "config": config, "repetition": run.repetition, "station": model_station["station"],
                "received": count, "goodput_mbps": count * params["L"] * 8 / MEASUREMENT_S / 1e6,
                "airtime_fraction": count * model_station["exchange_us"] / MEASUREMENT_S / 1e6,
                "delay_ms": 1000 * delay if delay is not None and math.isfinite(delay) else None,
            })
        rows.extend(per_station)
        goodput = [row["goodput_mbps"] for row in per_station]
        airtime = [row["airtime_fraction"] for row in per_station]
        rows.append({"config": config, "repetition": run.repetition, "station": "all",
                     "goodput_mbps": sum(goodput), "airtime_fraction": sum(airtime),
                     "jain_goodput": jain(goodput), "jain_airtime": jain(airtime)})
    return rows


def summarize(rows, stations):
    for config in CONFIGS:
        for name in [station["station"] for station in stations] + ["all"]:
            selected = [row for row in rows if row["config"] == config and row["station"] == name]
            metrics = ("goodput_mbps", "airtime_fraction")
            metrics += ("jain_goodput", "jain_airtime") if name == "all" else ("delay_ms",)
            for metric in metrics:
                yield {"config": config, "station": name, "metric": metric,
                       **estimate([row.get(metric) for row in selected])}


def compare(summary, stations, params, no_slow_mbps):
    values = {(row["config"], row["station"], row["metric"]): row["mean"] for row in summary}
    for config, model_key in (("DownlinkFifoRuns", "fifo"), ("DownlinkAirtimeRuns", "drr")):
        for station in stations:
            name = station["station"]
            for metric, key in (("goodput_mbps", "mbps"), ("delay_ms", "delay_ms")):
                observed = values[config, name, metric]
                predicted = station[f"{model_key}_{key}"]
                yield {"measure": f"{config} {name} {metric}", "model": predicted,
                       "observed": observed,
                       "change_pct": 100 * (observed / predicted - 1) if observed is not None else None}
    for measure, name in (("DRR total goodput gain", "all"), ("DRR slow station goodput change", "host[0]")):
        baseline = values["DownlinkFifoRuns", name, "goodput_mbps"]
        drr = values["DownlinkAirtimeRuns", name, "goodput_mbps"]
        yield {"measure": measure, "baseline": baseline, "observed": drr,
               "change_pct": 100 * (drr / baseline - 1) if baseline else None}
    yield {"measure": "FIFO fast goodput loss due to slow station",
           "baseline": no_slow_mbps, "model": stations[1]["fifo_mbps"],
           "change_pct": 100 * (stations[1]["fifo_mbps"] / no_slow_mbps - 1)}
    offered = params["L"] * 8 / 0.025 / 1e6
    for config in ("DownlinkFifoLight", "DownlinkAirtimeLight"):
        received = values[config, "host[0]", "goodput_mbps"]
        yield {"measure": f"{config} slow goodput vs mean offered load", "baseline": offered,
               "observed": received, "change_pct": 100 * (received / offered - 1)}
        fast = [values[config, station["station"], "goodput_mbps"] for station in stations[1:]]
        yield {"measure": f"{config} mean fast station goodput", "observed": mean(fast)}
    baseline_delay = values["DownlinkFifoLight", "host[0]", "delay_ms"]
    drr_delay = values["DownlinkAirtimeLight", "host[0]", "delay_ms"]
    yield {"measure": "Light slow station DRR vs FIFO delay (ms)",
           "baseline": baseline_delay, "observed": drr_delay,
           "change_pct": 100 * (drr_delay / baseline_delay - 1)
           if baseline_delay and drr_delay is not None else None}


def plot(summary, stations, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lookup = {(row["config"], row["station"], row["metric"]): row for row in summary}
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained")
    names = [station["station"] for station in stations]
    width = 0.15
    for ax, metric, label in zip(axes, ("goodput_mbps", "airtime_fraction"),
                                 ("Goodput (Mbps)", "Successful exchange airtime fraction")):
        for index, config in enumerate(CONFIGS):
            positions = [i + (index - 2) * width for i in range(len(names))]
            values = [lookup[config, name, metric] for name in names]
            ax.bar(positions, [row["mean"] for row in values], width=width,
                   yerr=[row["ci95_half"] or 0 for row in values], capsize=2, label=config)
            model_key = {"DownlinkFifoRuns": "fifo", "DownlinkAirtimeRuns": "drr"}.get(config)
            if model_key:
                suffix = "mbps" if metric == "goodput_mbps" else "airtime_fraction"
                ax.scatter(positions, [s[f"{model_key}_{suffix}"] for s in stations],
                           marker="D", s=24, color="black", zorder=3,
                           label="Analytical model" if model_key == "fifo" else None)
        ax.set_xticks(range(len(names)), names)
        ax.set_ylabel(label)
    axes[0].legend(fontsize=8)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=HERE / "results")
    parser.add_argument("--output", type=Path, default=HERE / "analysis")
    parser.add_argument("--model-only", action="store_true")
    args = parser.parse_args()
    params = load_params(HERE.parent / "params.json")
    stations, no_slow_mbps = model(params)
    write_table(args.output / "frame_headers.csv", ("component", "bytes"),
                [{"component": name, "bytes": size} for name, size in HEADERS.items()])
    write_table(args.output / "model.csv", stations[0].keys(), stations)
    trace = drr_trace([s["data_us"] for s in stations[:2]])
    write_table(args.output / "drr_first_ten.csv", trace[0].keys(), trace)
    if not args.model_only:
        rows = [row for config in CONFIGS for row in measure(config, args.results, params, stations)]
        summary = list(summarize(rows, stations))
        write_table(args.output / "runs.csv",
                    ("config", "repetition", "station", "received", "goodput_mbps", "airtime_fraction",
                     "delay_ms", "jain_goodput", "jain_airtime"), rows)
        write_table(args.output / "summary.csv", summary[0].keys(), summary)
        write_table(args.output / "comparisons.csv", ("measure", "baseline", "model", "observed", "change_pct"),
                    compare(summary, stations, params, no_slow_mbps))
        plot(summary, stations, args.output / "goodput_airtime.png")
    print(f"Phase 2 tables written to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(str(error))
