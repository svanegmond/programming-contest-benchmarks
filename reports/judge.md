# Waterloo judge environment and controls

All numbers below were measured on the judge host (2026-10-20, host untuned: governor `powersave`, boost on, SMT on, Ollama idle). This is a sanity pass, not the timing experiment.

## Image

- Arch Linux Archive date `2026/10/01` (`code/cco-eval/judge/ARCHIVE_DATE`); official bootstrap `archlinux-bootstrap-2026.10.01-x86_64.tar.zst`, SHA-256 `0c9c97b7a58a8a023c9e07f84cb307a9e8006e65d8fc4c17b83ddc8010b45c10` checked against the official `sha256sums.txt` on every build (`...tar.zst: OK`).
- Packages (`packages.txt`): base, base-devel, gcc 16.2.1, pypy3 8.0.0 (Python 3.12 runtime), python 3.14.7, git, pkgconf, libcap, libseccomp, systemd 262, linux 7.2.7, mkinitcpio, squashfs-tools. Isolate 2.7 (commit `8f185bb37f3f23e29b33b0c7727c91c13429abe3`) is built from source inside the rootfs. `pacman -Q` lists 182 packages.
- Output under `/srv/waterloo/image/`: read-only `root.squashfs` (about 619 MB), `vmlinuz-linux`, `initramfs.img` (virtio_mmio, virtio_pci, virtio_blk, virtio_console, squashfs), `pacman-Q.txt`, `env_id`, `manifest.txt`.
- `env_id` = sha256(`ARCHIVE_DATE` newline, `pacman -Q`) = `687bded99a1810ec00310a4c21a4dad52197abf869a07c61ec594f4cfee9d9f9`.
- The build re-execs itself under `unshare --mount --propagation private` so its chroot binds cannot affect the judge host's mount table.

### Rebuild reproducibility (same archive date)

Four full `build-image.sh` runs on the judge host (the image was rebuilt after each fix below). The first run's `pacman-Q.txt` was saved before the second run; builds 2 to 4 are each compared against it:

```text
cmp image/pacman-Q.txt judge-work/pacman-Q.build1.txt   -> identical (builds 2, 3 and 4: identical)
sha256 of pacman-Q.txt (both)  4ad42e34aacd22a5e7201f23cb75b0ac477d4435b0d6307f1eb30b92583e74dc
env_id (all builds)            687bded99a1810ec00310a4c21a4dad52197abf869a07c61ec594f4cfee9d9f9
```

The squashfs bytes are not claimed reproducible (timestamps, machine state); only the package set and `env_id` are.

### `env-id` from both arms

```text
python3 -m judge env-id --backend vm    -> {"env_id": "687bded9...d9f9"}   (read from /etc/waterloo-env-id inside the booted guest)
python3 -m judge env-id --backend host  -> {"env_id": "687bded9...d9f9"}   (mounted image marker, must match image/env_id)
```

Identical. The host arm refuses to run unless the image is a read-only mount.

## VM

`judge/vm.sh start|stop|status`. QEMU 10 `-machine microvm,accel=kvm -cpu host -smp 4 -m 4G`, no network device, root.squashfs as virtio-blk (read-only), Arch `linux` kernel plus a custom initramfs. **microvm booted; no q35 fallback was needed.** Control channel: a virtio-serial port (`org.waterloo.judge`) exposed as a host-local unix socket `judge-work/vm-control.sock`, spoken to by a Python agent started by systemd in the guest. Guest isolate gets a sanitized `/dev`; the control port is not visible inside a sandbox (checked: `judge_control_device_present False` in the network test).

Boot time, `vm.sh start` from qemu launch to first ping answered (cold, 6 boots): 5.911, 5.770, 5.720, 5.699, 5.714, 5.895 s (median 5.74 s). Most of it is systemd userspace boot.

Per-run overhead (hello world C++, 60 runs, fresh isolate box init + run + cleanup, minus isolate's own wall time; includes the vsock-style round trip for the VM):

```text
host: call_median 4.6 ms  call_p95 5.3 ms   overhead median 4.5 ms  p95 5.0 ms
vm:   call_median 5.3 ms  call_p95 5.6 ms   overhead median 5.3 ms  p95 5.5 ms
```

Both are far under the 50 ms p95 bar. Compile of hello world is 0.6 to 0.7 s on either arm.

## Sanity programs, both backends (`python3 -m judge run`)

Time limit in brackets; kill at 3x. cpu_s is isolate's cgroup CPU time (100 ms tick granularity on kills).

| program | host verdict | vm verdict | notes |
|---|---|---|---|
| hello C++ [2 s] | completed, cpu 0.00, 1.7 MB | completed, cpu 0.00, 1.7 MB | stdout `hello` |
| hello Python (pypy3) [2 s] | completed, cpu 0.043, 42.7 MB | completed, cpu 0.042, 42.8 MB | |
| C++ infinite loop [0.2 s] | killed, cpu 0.699, over_time | killed, cpu 0.698, over_time | wall-clock kill at 3x = 0.6 s |
| C++ 0.4 s busy loop [0.2 s] | completed, cpu 0.400, `over_time: true` | completed, cpu 0.399, `over_time: true` | the over-limit-but-under-kill case: normal verdict, tagged over_time |
| Python recursion depth 1e6 [3 s] | completed, cpu 0.91, 1033 MB, stdout `1000000` | completed, cpu 0.96, 1035 MB, stdout `1000000` | large stack works (stack cap = memory cap) |
| C++ touch 3 GB, `--memory-mb 512` | killed (cgroup OOM), peak 2048 MB, `over_memory: true` | killed (guest cgroup OOM, VM survived), peak 2048 MB, `over_memory: true` | 2048 MB hard cap enforced |
| Python `socket.connect(("1.1.1.1",53))` | completed; `OSError [Errno 101] Network is unreachable` | same | no network inside isolate in either arm; VM also has no NIC |

`run` returns `verdict` of `completed`, `runtime_error`, `killed` or `compile_error` (it has no expected output; `grade` assigns pass/wrong_answer). Raw JSON per case is in `judge-work/sanity/results/`. Note: a first version of the Python recursion and 3 GB programs was wrong (infinite recursion; allocation optimized out by `-O2`); the table is from the corrected programs.

## `grade` on an admitted problem

`cco-2016-d1p1` (from main's `problems/`, 19 tests inside `2016/2016CCOTestData.zip` read via `zip::member`), trivial wrong C++ solution printing `0 0`:

```text
python3 -m judge grade --backend host|vm --code wrong.cpp --lang cpp --problem problem.json --tests tests.jsonl \
   --zips-root /srv/waterloo/zips --attempt-id wrong-{host,vm} --out out-{host,vm}
verdict.json: all_pass false, partial_score 0, outcome "fail", env_id 687bded9...d9f9,
              subtask_pass {subtask_1: false, full_constraints: false}
test_results.jsonl: 19 rows (TestResult shape). Both arms: 17 wrong_answer, 2 pass (field_trip.6 and .17, whose official output is `0 0`, confirmed in the ZIP)
wall: host 0.40 s, vm 2.8 s (the VM arm ships the 6 large inputs through the serial channel)
```

Host and VM rows agree. Output is in `judge-work/sanity/grade/out-{host,vm}/`.

## Decisions and deviations

- Isolate `--time` is set to `kill_factor x statement_limit` instead of using `--extra-time`: isolate (checked in `isolate.c` v2.7) reports TO for any exit above `--time` regardless of `--extra-time`. `over_time` is cpu_s greater than the statement limit.
- Peak memory is the larger of isolate's `max-rss` and cgroup `cg-mem` (`memory.peak`); `over_memory` compares against the problem's limit when given.
- Output files are capped at 64 MiB per file (`--fsize`).
- `Problem` gained optional `absolute_tolerance` and `relative_tolerance` (default 1e-6 when absent; a `tolerance` key supplies both); this is an additive change to `cco_eval/waterloo.py`.
- Guest isolate config uses fixed `first_uid 60000`, 1000 boxes (matching the host's `/usr/local/etc/isolate`) because the default `subid_user` needs /etc/subuid.
- Guest `/var` is a tmpfs and `/etc/machine-id` is a fixed value, so a read-only root boots without first-boot presets.

