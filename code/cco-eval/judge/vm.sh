#!/usr/bin/env bash
set -euo pipefail

IMAGE_DIR=${WATERLOO_IMAGE_DIR:-/srv/waterloo/image}
WORK_DIR=${WATERLOO_JUDGE_WORK:-/srv/waterloo/judge-work}
SOCKET="$WORK_DIR/vm-control.sock"
PID_FILE="$WORK_DIR/vm.pid"
QEMU=${QEMU:-qemu-system-x86_64}
MACHINE=${WATERLOO_QEMU_MACHINE:-microvm}

ping_vm() {
  python3 - "$SOCKET" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(1)
try:
    s.connect(sys.argv[1])
    s.sendall(b'{"op":"ping"}\n')
    data = bytearray()
    while not data.endswith(b"\n"):
        block = s.recv(4096)
        if not block:
            raise RuntimeError("control channel closed")
        data.extend(block)
    print(json.loads(data).get("pong", False))
except (OSError, ValueError, RuntimeError):
    raise SystemExit(1)
PY
}

case "${1:-}" in
  start)
    mkdir -p "$WORK_DIR" "$IMAGE_DIR/mounted-root"
    [[ -r "$IMAGE_DIR/root.squashfs" && -r "$IMAGE_DIR/vmlinuz-linux" && -r "$IMAGE_DIR/initramfs.img" ]] || {
      echo "Arch judge image missing; run build-image.sh on the judge host first" >&2
      exit 1
    }
    command -v "$QEMU" >/dev/null || { echo "$QEMU is not installed" >&2; exit 1; }
    [[ -r /dev/kvm && -w /dev/kvm ]] || { echo "/dev/kvm is not accessible to $(id -un)" >&2; exit 1; }
    if mountpoint -q "$IMAGE_DIR/mounted-root"; then
      :
    else
      sudo mount -t squashfs -o loop,ro "$IMAGE_DIR/root.squashfs" "$IMAGE_DIR/mounted-root"
    fi
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      if ping_vm >/dev/null 2>&1; then
        echo "VM already running (pid $(cat "$PID_FILE"))"
        exit 0
      fi
      echo "VM process exists but guest control is not ready" >&2
      exit 1
    fi
    rm -f "$PID_FILE" "$SOCKET"
    start_time=$(python3 -c 'import time; print(time.monotonic())')
    if [[ "$MACHINE" == microvm ]]; then
      block_device=virtio-blk-device
      serial_device=virtio-serial-device
    elif [[ "$MACHINE" == q35 ]]; then
      block_device=virtio-blk-pci
      serial_device=virtio-serial-pci
    else
      echo "unsupported QEMU machine: $MACHINE (choose microvm or q35)" >&2
      exit 2
    fi
    nohup "$QEMU" \
      -machine "$MACHINE,accel=kvm" \
      -cpu host \
      -smp 4 \
      -m 4G \
      -nodefaults -no-user-config -display none -monitor none \
      -no-reboot \
      -kernel "$IMAGE_DIR/vmlinuz-linux" \
      -initrd "$IMAGE_DIR/initramfs.img" \
      -append "root=/dev/vda rootfstype=squashfs ro rootwait console=ttyS0 ${WATERLOO_KERNEL_APPEND:-}" \
      -drive "file=$IMAGE_DIR/root.squashfs,if=none,id=root,format=raw,readonly=on" \
      -device "$block_device,drive=root" \
      -serial "file:$WORK_DIR/vm-serial.log" \
      -chardev "socket,id=judge,path=$SOCKET,server=on,wait=off" \
      -device "$serial_device" \
      -device virtserialport,chardev=judge,name=org.waterloo.judge \
      > "$WORK_DIR/qemu.log" 2>&1 < /dev/null &
    qemu_pid=$!
    printf '%s\n' "$qemu_pid" > "$PID_FILE"
    deadline=$((SECONDS + 120))
    while (( SECONDS < deadline )); do
      if ! kill -0 "$qemu_pid" 2>/dev/null; then
        echo "QEMU exited before guest control was ready; see $WORK_DIR/qemu.log and vm-serial.log" >&2
        rm -f "$PID_FILE"
        exit 1
      fi
      if ping_vm >/dev/null 2>&1; then
        end_time=$(python3 -c 'import time; print(time.monotonic())')
        python3 - "$start_time" "$end_time" "$MACHINE" <<'PY'
import sys
print(f"VM ready; machine={sys.argv[3]}; boot_time_s={float(sys.argv[2]) - float(sys.argv[1]):.3f}")
PY
        echo "pid=$qemu_pid"
        exit 0
      fi
      sleep 0.25
    done
    echo "Timed out waiting for guest control; see $WORK_DIR/qemu.log and vm-serial.log" >&2
    exit 1
    ;;
  stop)
    if [[ ! -f "$PID_FILE" ]]; then
      echo "VM stopped"
      exit 0
    fi
    qemu_pid=$(cat "$PID_FILE")
    if kill -0 "$qemu_pid" 2>/dev/null; then
      kill -TERM "$qemu_pid"
      for _ in $(seq 1 40); do
        kill -0 "$qemu_pid" 2>/dev/null || break
        sleep 0.25
      done
      if kill -0 "$qemu_pid" 2>/dev/null; then
        echo "QEMU did not stop after SIGTERM (pid $qemu_pid)" >&2
        exit 1
      fi
    fi
    rm -f "$PID_FILE" "$SOCKET"
    echo "VM stopped"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && ping_vm >/dev/null 2>&1; then
      echo "VM running (pid $(cat "$PID_FILE"))"
    else
      echo "VM stopped or not ready"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
