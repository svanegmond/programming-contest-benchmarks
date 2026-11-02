# Waterloo timing proof, as-is (brief Sequence step 4)

**Verdict: the VM passes. Judge in the VM.** This report was revised after the Lead dropped the p99/median criterion. The bar is now CV at most 2%, VM median within 5% of host, and per-run overhead p95 under 50 ms. Nothing was rerun; the 200-run data below is re-evaluated against the new bar. Under the original bar the VM missed on one number (W1 p99/median 1.057 against 1.05), and that statistic is still reported below for information only.

Run count: the experiment ran 200 runs per cell. The script default is now 10 runs per cell (`--runs 10 --block 5`, with `--runs` and `--block` still settable). The 200-run CSVs stay as the record. The Ollama-busy arm is still deferred.

Claim being tested, in shipping words: "isolate CPU time for ~1 s programs on the judge host, untuned, has CV at most 2% in the host arm and in the VM arm; the VM median is within 5% of host; per-run overhead p95 is under 50 ms."

## Setup

- The judge host as-is: governor `powersave`, EPP `performance`, boost on, THP `always`, ASLR 2, no pinning, SMT on, 48 GB swap. Recorded at start and end in `timing/meta.json`; identical at both.
- Judge: `/srv/waterloo/src-timing` (this branch rsynced), image `env_id` `687bded9...d9f9`. Host arm and VM arm both report it.
- VM: the already running microvm (QEMU 10, microvm, `-cpu host -smp 4 -m 4G`, unpinned, pid 68785, left running).
- Host arm: `IsolateRunner` (box 0, `--cg`). VM arm: `VmRunner` over the virtio-serial socket, running the same `IsolateRunner` inside the guest. Both use `run_artifact`, so each timed run is isolate init, run and cleanup, plus the control round trip in the VM arm.
- Each workload is compiled once per arm (`g++ -O2 -static`, or pypy3 for Python), then run 10 times as a discarded warm-up, then 200 timed runs (the script default is now 10). The timed runs form 10 blocks of 20 per arm, with host and VM interleaved per workload and the arm order flipped every block. Total 1,600 timed runs.
- Workloads (kill limit 3x the stated limit; stdin empty):

| id | what | time on the judge host |
|---|---|---|
| W0_trivial | C++ `int main(){return 0;}` | 0.000 s CPU (isolate reports whole ms) |
| W1_sort_cpu | C++ sort of 20M uint32, fixed xorshift seed, prints `is_sorted` and a sampled hash | 1.17 s, 77 MB |
| W2_cache_chase | C++ dependent random walk, 10M steps over a 400 MB array (read and write) | 0.83 s, 383 MB |
| W3_pypy_loop | pypy3, integer loop, 1e9 iterations, prints the result | 0.94 s, 48 MB |

- Script: `code/cco-eval/judge/timing_proof.py` (`run` and `analyze`). Raw data: `timing/runs.csv` (one row per run: cpu, wall, orchestrator call time, overhead, returncode, killed, peak memory, stdout hash, max CPU frequency read right after the run), `blocks.csv` (per block: host busy fraction from `/proc/stat`, Ollama CPU ticks, load average), `meta.json`.


## Results (200 timed runs per cell)

CPU time is isolate's cgroup `time`. CV is stdev over mean. Overhead is orchestrator wall around `run_artifact` minus isolate's `time-wall`.

| workload | arm | errors | median cpu s | min | p99 | max | CV | p99/median | block-median range s | overhead p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| W0_trivial | host | 0 | 0.000 | 0.000 | 0.000 | 0.000 | n/a | n/a | n/a | 4.7 |
| W0_trivial | vm | 0 | 0.000 | 0.000 | 0.000 | 0.000 | n/a | n/a | n/a | 24.6 |
| W1_sort_cpu | host | 0 | 1.173 | 1.153 | 1.220 | 1.227 | 1.66% | 1.040 | 1.159 to 1.188 | 5.2 |
| W1_sort_cpu | vm | 0 | 1.168 | 1.157 | 1.234 | 1.237 | 1.72% | 1.057 | 1.161 to 1.185 | 25.1 |
| W2_cache_chase | host | 0 | 0.831 | 0.822 | 0.857 | 0.859 | 1.10% | 1.031 | 0.827 to 0.834 | 5.5 |
| W2_cache_chase | vm | 0 | 0.839 | 0.817 | 0.867 | 0.879 | 1.31% | 1.034 | 0.837 to 0.842 | 25.2 |
| W3_pypy_loop | host | 0 | 0.940 | 0.937 | 0.946 | 0.998 | 0.56% | 1.007 | 0.939 to 0.940 | 4.9 |
| W3_pypy_loop | vm | 0 | 0.941 | 0.938 | 0.948 | 0.951 | 0.20% | 1.007 | 0.941 to 0.942 | 24.5 |

Against the bar (`analyze` prints `decision: vm`; host also passes). Informational only, not judged: p99/median is at most 1.040 on host and 1.057 on the VM (W1).

| bar | host | VM |
|---|---|---|
| CV at most 2% (W1, W2, W3) | pass (max 1.66%) | pass (max 1.72%) |
| VM median within 5% of host | n/a | pass: W1 0.996x, W2 1.008x, W3 1.001x |
| per-run overhead p95 under 50 ms | pass (all workloads at most 5.5 ms) | pass (all at most 25.2 ms) |

Informational: bootstrap 95% intervals for p99/median (2,000 resamples of the 200 runs): W1 host 1.037 to 1.046, W1 VM 1.048 to 1.061, W2 host 1.024 to 1.034, W2 VM 1.026 to 1.049, W3 host 1.004 to 1.062, W3 VM 1.005 to 1.011. The host W3 interval is wide because one run took 0.998 s (isolated outlier).

Overhead on W0 specifically: host median 4.4 ms, p95 4.7, max 4.8; VM median 24.2, p95 24.6, max 25.1.

## Benchmark checklist

1. **Limiter.** Single-threaded CPU work at the boost clock. The max CPU frequency read after each run sat at 4.31 to 4.55 GHz for W1 (down to 3.6 GHz on a few W2 and W3 runs) and correlated with run time at |r| at most 0.15 in all six cells, so frequency does not explain the spread. The spread is mostly a slow drift shared by both arms: W1 block medians move 1.159 to 1.188 s in the host arm and 1.161 to 1.185 s in the VM arm, with the high blocks (4 and 5) high in both. Interleaving makes both arms see it. The VM-only W1 tail (7 of 200 runs above 1.05x median, none for host, 6 of them in blocks 4 to 6) is run-to-run noise from an unpinned guest. During VM runs `top -H` on the QEMU process showed the program's vCPU thread at about 100% and two other vCPU threads busy at 9 to 39%, so the guest scheduler spreads isolate work across the four unpinned vCPUs. I did not trace the cause further because pinning is out of scope until the Lead decides.
2. **Tuned.** Both arms use the same release build flags (`-O2 -static`, pypy3) and the same isolate arguments. The judge host itself is deliberately untuned per the brief. The result is valid for the judge host's current state only.
3. **Limits.** W2 peak memory 383 MB, under the 2048 MB cap; no kills. CPU times are consistent with wall (`wall` within about 1 ms of `cpu`). Nothing exceeds hardware limits.
4. **Errors.** 0 of 1,600 timed runs had a nonzero return code or kill. `analyze` refuses to report if any run errored or if outputs differ between runs or arms.
5. **Reproducibility.** 200 runs per cell in 10 alternating blocks of 20, median and range above.
6. **Matters.** CV of 1.7% on a 1 s run is 20 ms of noise against a 1 to 5 s limit, comfortably below the tolerances a judge needs. 
7. **Happened.** Each cell has exactly one distinct stdout hash, identical across host and VM (W1 prints `is_sorted` and a hash; W2 prints final index and sum; W3 prints the loop result). Peak memory (77, 383, 48 MB) matches what each program allocates.

Host noise check: the host busy fraction per block was 6.4 to 6.7% for W1 to W3 host blocks (one core of 16 is 6.25%), so nothing else was running on the judge host during the host runs. VM blocks read 11.7 to 11.9% (QEMU threads). Ollama used 1 CPU tick (0.01 s) across the whole experiment.

## Overhead caveat

The VM overhead is 24 to 25 ms in this experiment but 4.5 ms (p95 4.8) when I called `run_artifact` back to back with no gap (60 runs), and 13 ms after a 0.5 s idle gap; the host stays at 4.4 to 4.6 ms in all three cases (separate probe, run on the judge host after the experiment, not saved as data). The experiment reads 16 sysfs frequency files after every run, which leaves a short gap, and the guest seems to pay a wake-up cost after any gap. A real grading loop also has gaps, so the 24 to 25 ms is the more realistic figure; both pass the 50 ms bar. The judge report's 5.3 ms VM overhead was the back-to-back figure.

## Ollama

`curl -s localhost:11434/api/ps` is refused: Ollama listens on a LAN address only. At start and end, a `curl` to that address `/api/ps` returned `{"models":[]}` (idle, no model loaded), recorded in `meta.json`. I generated no Ollama load. **The Ollama-busy arm is deferred.**

## Not done

- Ollama-busy arm (deferred, by instruction).
- 30 cold VM boots (skipped by instruction; the judge report has 6, median 5.74 s).
- Boost-off arm, core pinning, SMT: not touched (as-is brief).
- W4 and W5 limit-kill checks from the study: not in this brief's four workloads. Kill behaviour is covered by the judge report's sanity table.

