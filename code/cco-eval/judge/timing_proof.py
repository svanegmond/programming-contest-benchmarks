"""As-is timing proof for the Waterloo judge: host isolate vs isolate in the microvm.

Run on the judge host from code/cco-eval:
    python3 -m judge.timing_proof run --out <dir> [--runs 10 --block 5 --warmup 10]
    python3 -m judge.timing_proof analyze --csv <dir>/runs.csv
The judge host is measured untuned (governor, boost, pinning are not touched).
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import statistics
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .core import IsolateRunner, VmRunner

ARMS = ("host", "vm")
KILL_FACTOR = 3.0

BAR_CV = 0.02
BAR_VM_MEDIAN_RATIO = 1.05
BAR_OVERHEAD_P95_S = 0.050

OLLAMA_URLS = tuple(dict.fromkeys(("http://localhost:11434/api/ps", os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/ps")))

SORT_CPP = """
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <vector>
int main() {
    const size_t n = %(n)d;
    std::vector<uint32_t> a(n);
    uint64_t s = 88172645463325252ULL;
    for (auto &x : a) { s ^= s << 13; s ^= s >> 7; s ^= s << 17; x = (uint32_t)(s >> 32); }
    std::sort(a.begin(), a.end());
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i += 4093) h = (h ^ a[i]) * 1099511628211ULL;
    printf("%%d %%llu\\n", (int)std::is_sorted(a.begin(), a.end()), (unsigned long long)h);
}
"""

CHASE_CPP = """
#include <cstdint>
#include <cstdio>
#include <vector>
int main() {
    const uint64_t n = %(n)d;
    std::vector<uint32_t> a(n);
    for (uint64_t i = 0; i < n; i++) a[i] = (uint32_t)(i * 2654435761u);
    uint64_t idx = 12345, acc = 0;
    for (uint64_t step = 0; step < %(steps)d; step++) {
        uint32_t v = a[idx];
        acc += v;
        a[idx] = v + 1;
        idx = (v + idx * 6364136223846793005ULL + 1442695040888963407ULL) %% n;
    }
    printf("%%llu %%llu\\n", (unsigned long long)idx, (unsigned long long)acc);
}
"""

LOOP_PY = """
s = 0
for i in range(%(n)d):
    s = ((s + i * i) ^ (i >> 3)) & 0xFFFFFFFF
print(s)
"""

EMPTY_CPP = "int main() { return 0; }\n"


@dataclass(frozen=True)
class Workload:
    id: str
    lang: str
    source: str
    limit_s: float


def workloads(sort_n: int, chase_steps: int, loop_n: int) -> list[Workload]:
    return [
        Workload("W0_trivial", "cpp", EMPTY_CPP, 2.0),
        Workload("W1_sort_cpu", "cpp", SORT_CPP % {"n": sort_n}, 5.0),
        Workload("W2_cache_chase", "cpp", CHASE_CPP % {"n": 100_000_000, "steps": chase_steps}, 5.0),
        Workload("W3_pypy_loop", "python", LOOP_PY % {"n": loop_n}, 5.0),
    ]


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of empty sequence")
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def coefficient_of_variation(values: Sequence[float]) -> float | None:
    mean = statistics.fmean(values)
    if len(values) < 2 or mean == 0:
        return None
    return statistics.stdev(values) / mean


def schedule(workload_ids: Sequence[str], runs: int, block: int) -> list[tuple[int, str, str, int]]:
    """(block index, workload, arm, runs in block); arm order alternates each block."""
    if runs % block:
        raise ValueError("runs must be a multiple of the block size")
    plan = []
    for index in range(runs // block):
        arms = ARMS if index % 2 == 0 else ARMS[::-1]
        for workload in workload_ids:
            for arm in arms:
                plan.append((index, workload, arm, block))
    return plan


def _proc_stat() -> tuple[int, int]:
    fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return sum(fields), fields[3] + fields[4]


def _ollama_ticks() -> int:
    total = 0
    for path in glob.glob("/proc/[0-9]*/stat"):
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        name = text[text.index("(") + 1 : text.rindex(")")]
        if name.startswith("ollama"):
            parts = text[text.rindex(")") + 2 :].split()
            total += int(parts[11]) + int(parts[12])
    return total


def _max_freq_khz() -> int:
    best = 0
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            best = max(best, int(Path(path).read_text()))
        except (OSError, ValueError):
            continue
    return best


def ollama_state() -> dict[str, str]:
    state = {}
    for url in OLLAMA_URLS:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                state[url] = response.read().decode()
        except OSError as error:
            state[url] = f"unreachable: {error}"
    return state


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return f"unreadable: {error}"


def host_state() -> dict[str, object]:
    cpu = "/sys/devices/system/cpu"
    return {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "loadavg": _read("/proc/loadavg"),
        "governor_cpu0": _read(f"{cpu}/cpu0/cpufreq/scaling_governor"),
        "boost": _read(f"{cpu}/cpufreq/boost"),
        "epp_cpu0": _read(f"{cpu}/cpu0/cpufreq/energy_performance_preference"),
        "thp": _read("/sys/kernel/mm/transparent_hugepage/enabled"),
        "aslr": _read("/proc/sys/kernel/randomize_va_space"),
        "ollama_ps": ollama_state(),
        "ollama_cpu_ticks": _ollama_ticks(),
        "vm_pid": _read(os.environ.get("WATERLOO_JUDGE_WORK", "/srv/waterloo/judge-work") + "/vm.pid"),
    }


def _runner(arm: str) -> IsolateRunner | VmRunner:
    return IsolateRunner() if arm == "host" else VmRunner()


def run_experiment(out: Path, runs: int, block: int, warmup: int, workload_list: list[Workload]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    runners = {arm: _runner(arm) for arm in ARMS}
    meta = {"start": host_state(), "runs": runs, "block": block, "warmup": warmup, "arms": list(ARMS)}
    meta["env_id"] = {
        "host": (runners["host"].rootfs / "etc/waterloo-env-id").read_text().strip(),
        "vm": runners["vm"].request({"op": "env-id"})["env_id"],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))

    artifacts: dict[tuple[str, str], Path] = {}
    scratch = Path(tempfile.mkdtemp(prefix="timing-proof-"))
    by_id = {w.id: w for w in workload_list}
    for workload in workload_list:
        for arm in ARMS:
            compiled = runners[arm].prepare(
                workload.lang, workload.source.encode(), scratch / f"{arm}-{workload.id}"
            )
            if not compiled["ok"]:
                raise RuntimeError(f"{arm} compile of {workload.id} failed: {compiled['stderr']}")
            artifacts[(arm, workload.id)] = Path(compiled["artifact"])

    def execute(arm: str, workload: Workload) -> dict[str, object]:
        started = time.monotonic()
        result = runners[arm].run_artifact(
            workload.lang, artifacts[(arm, workload.id)], b"", workload.limit_s, KILL_FACTOR
        )
        call_s = time.monotonic() - started
        freq = _max_freq_khz()
        return {
            "cpu_s": result["cpu_s"],
            "wall_s": result["wall_s"],
            "call_s": call_s,
            "overhead_s": call_s - result["wall_s"] if result["wall_s"] is not None else None,
            "returncode": result["returncode"],
            "killed": int(bool(result["killed"])),
            "peak_mb": result["peak_memory_mb"],
            "stdout_sha": hashlib.sha256(result["stdout"]).hexdigest()[:16],
            "freq_max_khz_after": freq,
        }

    run_fields = [
        "phase", "block", "workload", "arm", "i", "cpu_s", "wall_s", "call_s", "overhead_s",
        "returncode", "killed", "peak_mb", "stdout_sha", "freq_max_khz_after",
    ]
    block_fields = ["block", "workload", "arm", "host_busy_frac", "ollama_ticks", "loadavg1_start"]
    with (out / "runs.csv").open("w", newline="") as run_file, (out / "blocks.csv").open(
        "w", newline=""
    ) as block_file:
        run_writer = csv.DictWriter(run_file, run_fields)
        block_writer = csv.DictWriter(block_file, block_fields)
        run_writer.writeheader()
        block_writer.writeheader()
        for workload in workload_list:
            for arm in ARMS:
                for i in range(warmup):
                    run_writer.writerow(
                        {"phase": "warmup", "block": -1, "workload": workload.id, "arm": arm, "i": i,
                         **execute(arm, workload)}
                    )
        for index, workload_id, arm, count in schedule([w.id for w in workload_list], runs, block):
            total0, idle0 = _proc_stat()
            ticks0 = _ollama_ticks()
            load1 = _read("/proc/loadavg").split()[0]
            for i in range(count):
                run_writer.writerow(
                    {"phase": "measure", "block": index, "workload": workload_id, "arm": arm, "i": i,
                     **execute(arm, by_id[workload_id])}
                )
            total1, idle1 = _proc_stat()
            block_writer.writerow(
                {"block": index, "workload": workload_id, "arm": arm,
                 "host_busy_frac": round(1 - (idle1 - idle0) / (total1 - total0), 4),
                 "ollama_ticks": _ollama_ticks() - ticks0, "loadavg1_start": load1}
            )
            run_file.flush()
            block_file.flush()
    meta["end"] = host_state()
    (out / "meta.json").write_text(json.dumps(meta, indent=2))


def _floats(rows: Iterable[dict[str, str]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r[key] not in ("", "None")]


def summarize(rows: list[dict[str, str]]) -> dict[str, dict[str, dict[str, object]]]:
    """Per workload and arm: CPU-time and overhead statistics over measured, non-warmup rows."""
    summary: dict[str, dict[str, dict[str, object]]] = {}
    for workload in dict.fromkeys(r["workload"] for r in rows):
        summary[workload] = {}
        for arm in ARMS:
            sel = [r for r in rows if r["workload"] == workload and r["arm"] == arm and r["phase"] == "measure"]
            cpu = _floats(sel, "cpu_s")
            overhead = _floats(sel, "overhead_s")
            median = statistics.median(cpu)
            block_medians = [
                statistics.median(_floats([r for r in sel if r["block"] == b], "cpu_s"))
                for b in sorted({r["block"] for r in sel}, key=int)
            ]
            summary[workload][arm] = {
                "n": len(sel),
                "errors": sum(1 for r in sel if r["killed"] != "0" or r["returncode"] != "0"),
                "distinct_outputs": len({r["stdout_sha"] for r in sel}),
                "output_sha": sorted({r["stdout_sha"] for r in sel})[0],
                "cpu_median_s": median,
                "cpu_min_s": min(cpu),
                "cpu_p95_s": percentile(cpu, 0.95),
                "cpu_p99_s": percentile(cpu, 0.99),
                "cpu_max_s": max(cpu),
                "cv": coefficient_of_variation(cpu),
                "p99_over_median": percentile(cpu, 0.99) / median if median else None,
                "block_median_range_s": [min(block_medians), max(block_medians)],
                "overhead_median_s": statistics.median(overhead),
                "overhead_p95_s": percentile(overhead, 0.95),
                "wall_median_s": statistics.median(_floats(sel, "wall_s")),
            }
    return summary


def _stable(arm_stats: dict[str, object]) -> bool:
    return arm_stats["cv"] is not None and arm_stats["cv"] <= BAR_CV


def judge_bar(summary: dict[str, dict[str, dict[str, object]]]) -> dict[str, object]:
    """Pass bar: CV, VM median ratio, overhead p95. p99/median is reported, not judged.

    W0 (cpu time below tick resolution) only feeds overhead.
    """
    timed = [w for w in summary if not w.startswith("W0")]
    overhead_ok = {
        arm: all(summary[w][arm]["overhead_p95_s"] < BAR_OVERHEAD_P95_S for w in summary) for arm in ARMS
    }
    ratios = {w: summary[w]["vm"]["cpu_median_s"] / summary[w]["host"]["cpu_median_s"] for w in timed}
    arm_pass = {
        arm: all(_stable(summary[w][arm]) for w in timed) and overhead_ok[arm] for arm in ARMS
    }
    outputs_agree = all(
        summary[w]["vm"]["output_sha"] == summary[w]["host"]["output_sha"]
        and all(summary[w][arm]["distinct_outputs"] == 1 and summary[w][arm]["errors"] == 0 for arm in ARMS)
        for w in summary
    )
    if not outputs_agree:
        raise RuntimeError("runs errored or outputs differ between runs or arms; timings measured other work")
    vm_ok = arm_pass["vm"] and all(abs(r - 1) <= BAR_VM_MEDIAN_RATIO - 1 for r in ratios.values())
    if vm_ok:
        decision = "vm"
    elif arm_pass["host"]:
        decision = "host"
    else:
        decision = "lead"
    return {
        "vm_median_ratio": ratios,
        "outputs_agree_no_errors": outputs_agree,
        "overhead_ok": overhead_ok,
        "arm_stability_and_overhead_pass": arm_pass,
        "vm_passes_bar": vm_ok,
        "decision": decision,
    }


def _fmt(value: object, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render(summary: dict[str, dict[str, dict[str, object]]], verdict: dict[str, object]) -> str:
    lines = [
        "| workload | arm | n | errors | outputs | median cpu s | min | p99 | max | CV | p99/median | block-median range s | overhead p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for workload, arms in summary.items():
        for arm, s in arms.items():
            low, high = s["block_median_range_s"]
            lines.append(
                f"| {workload} | {arm} | {s['n']} | {s['errors']} | {s['distinct_outputs']} | "
                f"{_fmt(s['cpu_median_s'], 3)} | {_fmt(s['cpu_min_s'], 3)} | {_fmt(s['cpu_p99_s'], 3)} | "
                f"{_fmt(s['cpu_max_s'], 3)} | {_fmt(s['cv'] and s['cv'] * 100, 2)}% | "
                f"{_fmt(s['p99_over_median'], 3)} | {low:.3f} to {high:.3f} | "
                f"{s['overhead_p95_s'] * 1000:.1f} |"
            )
    lines += ["", "```json", json.dumps(verdict, indent=2), "```"]
    return "\n".join(lines)


def analyze(csv_path: Path) -> str:
    with csv_path.open() as source:
        rows = list(csv.DictReader(source))
    summary = summarize(rows)
    return render(summary, judge_bar(summary))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m judge.timing_proof")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--runs", type=int, default=10)
    run.add_argument("--block", type=int, default=5)
    run.add_argument("--warmup", type=int, default=10)
    run.add_argument("--sort-n", type=int, default=int(os.environ.get("TP_SORT_N", 20_000_000)))
    run.add_argument("--chase-steps", type=int, default=int(os.environ.get("TP_CHASE_STEPS", 10_000_000)))
    run.add_argument("--loop-n", type=int, default=int(os.environ.get("TP_LOOP_N", 1_000_000_000)))
    check = commands.add_parser("analyze")
    check.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        run_experiment(
            args.out, args.runs, args.block, args.warmup, workloads(args.sort_n, args.chase_steps, args.loop_n)
        )
    else:
        print(analyze(args.csv))


if __name__ == "__main__":
    main()
