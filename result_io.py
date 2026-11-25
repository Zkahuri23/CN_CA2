"""Read native OMNeT++ result files and write analysis tables."""

import csv
import json
import shlex
from dataclasses import dataclass, field
from math import isfinite, sqrt
from statistics import mean, stdev


@dataclass
class Run:
    name: str
    repetition: int | None = None
    scalars: dict = field(default_factory=dict)
    statistics: dict = field(default_factory=dict)
    vectors: dict = field(default_factory=dict)


def read_file(path, wanted):
    runs = []
    run = None
    statistic = None
    vectors_by_id = {}
    in_header = False
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            kind = parts[0]
            if kind.isdigit():
                if kind in vectors_by_id:
                    points, time_col, value_col = vectors_by_id[kind]
                    points.append((float(parts[time_col]), float(parts[value_col])))
                continue
            if kind not in {"run", "attr", "scalar", "statistic", "field", "vector"}:
                continue
            if kind == "attr":
                if in_header and parts[1] == "repetition":
                    run.repetition = int(parts[2].strip('"'))
                continue
            tokens = shlex.split(line)
            if kind == "run":
                run = Run(tokens[1])
                runs.append(run)
                in_header = True
                statistic = None
                vectors_by_id = {}
                continue
            in_header = False
            if kind == "field":
                if statistic is not None and tokens[1] in {"count", "mean", "max"}:
                    statistic[tokens[1]] = float(tokens[2])
                continue
            statistic = None
            if kind == "scalar":
                run.scalars[tokens[1], tokens[2]] = float(tokens[3])
            elif kind == "statistic":
                statistic = run.statistics.setdefault((tokens[1], tokens[2]), {})
            elif kind == "vector":
                _, identifier, module, name, *columns = tokens
                key = (module, name)
                if key in wanted:
                    layout = columns[0] if columns else "TV"
                    points = run.vectors.setdefault(key, [])
                    vectors_by_id[identifier] = (points, 1 + layout.index("T"), 1 + layout.index("V"))
    if not runs:
        raise ValueError(f"No run found in {path}")
    return runs


def read_runs(directory, config, extension, wanted=()):
    paths = sorted(directory.glob(f"{config}-*.{extension}"))
    if not paths:
        raise ValueError(f"No {config}-*.{extension} in {directory}. Run {config} first.")
    runs = [run for path in paths for run in read_file(path, wanted)]
    repetitions = [run.repetition for run in runs]
    if None in repetitions or len(set(repetitions)) != len(runs):
        raise ValueError(f"Missing or duplicate repetition numbers for {config} in {directory}")
    return sorted(runs, key=lambda run: run.repetition)


def load_params(path):
    if not path.exists():
        raise ValueError(f"{path} is missing. Run ca2_params.py with your student IDs first.")
    return json.loads(path.read_text(encoding="utf-8"))


def estimate(values):
    """Mean and half-width of the two-sided Student t 95% interval."""
    values = [value for value in values if value is not None and isfinite(value)]
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if n else None, "ci95_half": None}
    from scipy.stats import t

    return {"n": n, "mean": mean(values), "ci95_half": t.ppf(0.975, n - 1) * stdev(values) / sqrt(n)}


def write_table(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
