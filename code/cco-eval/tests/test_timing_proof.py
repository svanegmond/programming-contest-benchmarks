import csv
import random

import pytest

from judge.timing_proof import ARMS, analyze, coefficient_of_variation, percentile, schedule

FIELDS = [
    "phase", "block", "workload", "arm", "i", "cpu_s", "wall_s", "call_s", "overhead_s",
    "returncode", "killed", "peak_mb", "stdout_sha", "freq_max_khz_after",
]


def _write(path, spec):
    """spec: {(workload, arm): (cpu samples, overhead_s, stdout sha)}"""
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        for (workload, arm), (cpu, overhead, sha) in spec.items():
            for i, value in enumerate(cpu):
                writer.writerow({
                    "phase": "measure", "block": i % 2, "workload": workload, "arm": arm, "i": i,
                    "cpu_s": value, "wall_s": value, "call_s": value + overhead, "overhead_s": overhead,
                    "returncode": 0, "killed": 0, "peak_mb": 1, "stdout_sha": sha, "freq_max_khz_after": 0,
                })


def _spec(vm_scale=1.0, vm_noise=0.001, vm_overhead=0.005):
    rng = random.Random(1)
    spec = {("W0_trivial", arm): ([0.0] * 40, vm_overhead if arm == "vm" else 0.004, "e3b0") for arm in ARMS}
    for workload in ("W1", "W2", "W3"):
        spec[(workload, "host")] = ([1 + rng.gauss(0, 0.001) for _ in range(40)], 0.004, "abc")
        spec[(workload, "vm")] = ([vm_scale + rng.gauss(0, vm_noise) for _ in range(40)], vm_overhead, "abc")
    return spec


def test_percentile_and_cv():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.99) == 5
    assert coefficient_of_variation([2, 2, 2]) == 0
    assert coefficient_of_variation([0, 0]) is None


def test_schedule_alternates_arm_order_and_gives_each_arm_equal_runs():
    plan = schedule(["a", "b"], runs=40, block=20)
    assert [(w, arm) for index, w, arm, _ in plan if index == 0] == [
        ("a", "host"), ("a", "vm"), ("b", "host"), ("b", "vm")
    ]
    assert [(w, arm) for index, w, arm, _ in plan if index == 1][:2] == [("a", "vm"), ("a", "host")]
    for arm in ARMS:
        assert sum(count for _, w, a, count in plan if w == "a" and a == arm) == 40
    with pytest.raises(ValueError):
        schedule(["a"], runs=30, block=20)


def test_stable_vm_within_five_percent_chooses_vm(tmp_path):
    _write(tmp_path / "runs.csv", _spec())
    assert '"decision": "vm"' in analyze(tmp_path / "runs.csv")


def test_vm_noisy_or_slow_falls_back_to_host(tmp_path):
    _write(tmp_path / "noisy.csv", _spec(vm_noise=0.05))
    assert '"decision": "host"' in analyze(tmp_path / "noisy.csv")
    _write(tmp_path / "slow.csv", _spec(vm_scale=1.08))
    assert '"decision": "host"' in analyze(tmp_path / "slow.csv")


def test_both_arms_failing_goes_to_lead(tmp_path):
    spec = _spec()
    rng = random.Random(2)
    spec[("W1", "host")] = ([1 + rng.gauss(0, 0.1) for _ in range(40)], 0.004, "abc")
    spec[("W1", "vm")] = ([1 + rng.gauss(0, 0.1) for _ in range(40)], 0.005, "abc")
    _write(tmp_path / "runs.csv", spec)
    assert '"decision": "lead"' in analyze(tmp_path / "runs.csv")


def test_overhead_over_fifty_ms_fails_the_vm(tmp_path):
    _write(tmp_path / "runs.csv", _spec(vm_overhead=0.08))
    assert '"decision": "host"' in analyze(tmp_path / "runs.csv")


def test_differing_outputs_between_arms_refuse_to_report(tmp_path):
    spec = _spec()
    spec[("W1", "vm")] = (spec[("W1", "vm")][0], 0.005, "different")
    _write(tmp_path / "runs.csv", spec)
    with pytest.raises(RuntimeError):
        analyze(tmp_path / "runs.csv")


def test_p99_tail_alone_does_not_fail_the_vm(tmp_path):
    spec = _spec()
    cpu = list(spec[("W1", "vm")][0])
    cpu[:2] = [1.06, 1.06]
    spec[("W1", "vm")] = (cpu, 0.005, "abc")
    _write(tmp_path / "runs.csv", spec)
    assert '"decision": "vm"' in analyze(tmp_path / "runs.csv")
