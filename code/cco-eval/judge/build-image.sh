#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ARCHIVE_DATE=$(tr -d '\n' < "$HERE/ARCHIVE_DATE")
[[ "$ARCHIVE_DATE" =~ ^[0-9]{4}/[0-9]{2}/[0-9]{2}$ ]] || { echo "invalid ARCHIVE_DATE: $ARCHIVE_DATE" >&2; exit 2; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root (for example: sudo $0)" >&2
  exit 2
fi

# The judge host's / has shared propagation; unmounting chroot binds in the host namespace
# once tore down /sys/fs/cgroup, /dev/pts and /dev/shm host-wide (2026-10-20).
if [[ -z "${WL_PRIVATE_MOUNTNS:-}" ]]; then
  exec unshare --mount --propagation private env WL_PRIVATE_MOUNTNS=1 "$0" "$@"
fi

BASE=${WATERLOO_IMAGE_DIR:-/srv/waterloo/image}
WORK="$BASE/build"
ROOT="$WORK/rootfs"
DOWNLOADS="$WORK/downloads"
PACKAGE_CACHE="$WORK/package-cache"
RESOLV_BACKUP="$WORK/resolv.conf.bootstrap"
RESOLV_SAVED=0
VM_PID_FILE=${WATERLOO_JUDGE_WORK:-/srv/waterloo/judge-work}/vm.pid
if [[ -f "$VM_PID_FILE" ]] && kill -0 "$(cat "$VM_PID_FILE")" 2>/dev/null; then
  echo "stop the Waterloo VM before rebuilding its image" >&2
  exit 1
fi
if mountpoint -q "$BASE/mounted-root"; then
  umount "$BASE/mounted-root"
fi
mkdir -p "$DOWNLOADS"
if [[ -d "$ROOT/etc/pacman.d/gnupg" ]]; then
  chroot "$ROOT" gpgconf --homedir /etc/pacman.d/gnupg --kill gpg-agent >/dev/null 2>&1 || true
fi
rm -rf -- "$ROOT"
mkdir -p "$ROOT"

BOOTSTRAP_BASE=https://geo.mirror.pkgbuild.com/iso/latest
INDEX="$DOWNLOADS/index.html"
curl -fsSL "$BOOTSTRAP_BASE/" -o "$INDEX"
BOOTSTRAP=$(python3 - "$INDEX" <<'PY'
from html.parser import HTMLParser
import re, sys

class Links(HTMLParser):
    values = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if re.fullmatch(r"archlinux-bootstrap-[0-9.]+-x86_64\.tar\.zst", href):
                self.values.append(href)

links = Links()
links.feed(open(sys.argv[1], encoding="utf-8").read())
if not links.values:
    raise SystemExit("no official x86_64 bootstrap tarball found in latest/ listing")
print(sorted(links.values)[-1])
PY
)
SUMS="$DOWNLOADS/sha256sums.txt"
curl -fsSL "$BOOTSTRAP_BASE/sha256sums.txt" -o "$SUMS"
grep -E "^[[:xdigit:]]{64}[[:space:]]+\*?${BOOTSTRAP//./\\.}$" "$SUMS" > "$DOWNLOADS/bootstrap.sha256" || {
  echo "official sha256sums.txt has no entry for $BOOTSTRAP" >&2
  exit 1
}
curl -fsSL "$BOOTSTRAP_BASE/$BOOTSTRAP" -o "$DOWNLOADS/$BOOTSTRAP"
(cd "$DOWNLOADS" && sha256sum --check bootstrap.sha256)
tar --zstd -xf "$DOWNLOADS/$BOOTSTRAP" -C "$ROOT" --strip-components=1

MOUNTS=()
cleanup() {
  if [[ -d "$ROOT/etc/pacman.d/gnupg" ]]; then
    chroot "$ROOT" gpgconf --homedir /etc/pacman.d/gnupg --kill gpg-agent >/dev/null 2>&1 || true
  fi
  if (( RESOLV_SAVED )) && [[ -e "$RESOLV_BACKUP" ]]; then
    rm -f "$ROOT/etc/resolv.conf"
    cp -a "$RESOLV_BACKUP" "$ROOT/etc/resolv.conf"
    RESOLV_SAVED=0
  fi
  for ((i=${#MOUNTS[@]}-1; i>=0; i--)); do
    target=${MOUNTS[$i]}
    if mountpoint -q "$target"; then
      if [[ "$target" == "$ROOT/dev" ]] && mountpoint -q "$target/pts"; then
        umount -l "$target/pts"
      fi
      umount -R "$target" || umount -R -l "$target"
    fi
  done
}
trap cleanup EXIT
mount --bind "$ROOT" "$ROOT"
MOUNTS+=("$ROOT")
for target in dev proc sys; do
  mount --bind "/$target" "$ROOT/$target"
  MOUNTS+=("$ROOT/$target")
done

cat > "$ROOT/etc/pacman.d/mirrorlist" <<EOF
Server = https://archive.archlinux.org/repos/$ARCHIVE_DATE/\$repo/os/\$arch
EOF
cp -a "$ROOT/etc/resolv.conf" "$RESOLV_BACKUP"
RESOLV_SAVED=1
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"

# The bootstrap package set can be newer than the snapshot. -uu makes the
# resulting package inventory derive from the archive rather than the tarball.
chroot "$ROOT" pacman-key --init
chroot "$ROOT" pacman-key --populate archlinux
chroot "$ROOT" pacman -Syyuu --noconfirm
mapfile -t PACKAGES < <(grep -Ev '^[[:space:]]*(#|$)' "$HERE/packages.txt" | sed '/^$/d' | grep -v '^#')
mkdir -p "$PACKAGE_CACHE" "$ROOT/var/cache/pacman/pkg"
cp -an "$PACKAGE_CACHE/." "$ROOT/var/cache/pacman/pkg/"
chroot "$ROOT" pacman -Syyuu --noconfirm --needed "${PACKAGES[@]}"
cp -an "$ROOT/var/cache/pacman/pkg/." "$PACKAGE_CACHE/"
rm -rf "$ROOT/var/cache/pacman/pkg"/*
chroot "$ROOT" gpgconf --homedir /etc/pacman.d/gnupg --kill gpg-agent
rm -rf "$ROOT/etc/pacman.d/gnupg"
rm -f "$ROOT/etc/resolv.conf"
cp -a "$RESOLV_BACKUP" "$ROOT/etc/resolv.conf"
RESOLV_SAVED=0

ISO_SRC="$WORK/isolate"
rm -rf -- "$ISO_SRC"
git clone --quiet --depth 1 --branch v2.7 https://github.com/ioi/isolate.git "$ISO_SRC"
[[ $(git -C "$ISO_SRC" rev-parse HEAD) == 8f185bb37f3f23e29b33b0c7727c91c13429abe3 ]] || {
  echo "isolate v2.7 resolved to an unexpected commit" >&2
  exit 1
}
cp -a "$ISO_SRC" "$ROOT/opt/isolate-src"
chroot "$ROOT" /bin/bash -lc 'cd /opt/isolate-src && make -s isolate isolate-check-environment isolate-cg-keeper default.cf systemd/isolate.service systemd/isolate.slice && make -s install'
chroot "$ROOT" systemctl --root=/ enable isolate.service
sed -i -e 's/^subid_user = isolate$/first_uid = 60000\nfirst_gid = 60000\nnum_boxes = 1000/' "$ROOT/usr/local/etc/isolate"
rm -rf "$ROOT/opt/isolate-src"

mkdir -p "$ROOT/opt/waterloo/judge" "$ROOT/usr/local/lib/systemd/system" "$ROOT/var/local/lib/isolate" "$ROOT/var/log/journal"
cp "$HERE/agent.py" "$HERE/core.py" "$ROOT/opt/waterloo/judge/"
JUDGE_DEV="$ROOT/usr/share/waterloo/judge-dev"
mkdir -p "$JUDGE_DEV"
mknod -m 666 "$JUDGE_DEV/null" c 1 3
mknod -m 666 "$JUDGE_DEV/zero" c 1 5
mknod -m 666 "$JUDGE_DEV/random" c 1 8
mknod -m 666 "$JUDGE_DEV/urandom" c 1 9
chmod 666 "$JUDGE_DEV/null" "$JUDGE_DEV/zero" "$JUDGE_DEV/random" "$JUDGE_DEV/urandom"
ln -s /proc/self/fd "$JUDGE_DEV/fd"
ln -s /proc/self/fd/0 "$JUDGE_DEV/stdin"
ln -s /proc/self/fd/1 "$JUDGE_DEV/stdout"
ln -s /proc/self/fd/2 "$JUDGE_DEV/stderr"
cat > "$ROOT/usr/local/lib/systemd/system/waterloo-judge-agent.service" <<'EOF'
[Unit]
Description=Waterloo judge guest control agent
After=isolate.service
Requires=isolate.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/waterloo/judge/agent.py
Restart=on-failure
RestartSec=1
StandardInput=null
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chroot "$ROOT" systemctl --root=/ enable waterloo-judge-agent.service

mkdir -p "$ROOT/etc/systemd/system"
cat > "$ROOT/etc/systemd/system/var-local-lib-isolate.mount" <<'EOF'
[Unit]
Description=Volatile isolate workspaces
Before=isolate.service

[Mount]
What=tmpfs
Where=/var/local/lib/isolate
Type=tmpfs
Options=mode=0755,size=2G

[Install]
WantedBy=local-fs.target
EOF
chroot "$ROOT" systemctl --root=/ enable var-local-lib-isolate.mount
printf "%s\n" 77617465726c6f6f6a75646765000000 > "$ROOT/etc/machine-id"
cat > "$ROOT/etc/fstab" <<'EOF'
tmpfs /tmp tmpfs nosuid,nodev,mode=1777 0 0
tmpfs /var tmpfs nosuid,nodev,mode=0755 0 0
EOF

mkdir -p "$BASE"
cp "$ROOT/boot/vmlinuz-linux" "$BASE/vmlinuz-linux"
cat > "$WORK/mkinitcpio.conf" <<'EOF'
MODULES=(virtio_mmio virtio_pci virtio_blk virtio_console squashfs)
BINARIES=()
FILES=()
HOOKS=(base udev modconf block filesystems)
COMPRESSION="zstd"
EOF
cp "$WORK/mkinitcpio.conf" "$ROOT/etc/mkinitcpio-waterloo.conf"
KVER=$(find "$ROOT/usr/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)
[[ -n "$KVER" ]] || { echo "linux package installed no kernel modules" >&2; exit 1; }
chroot "$ROOT" mkinitcpio -k "$KVER" -c /etc/mkinitcpio-waterloo.conf -g /boot/initramfs-waterloo.img
cp "$ROOT/boot/initramfs-waterloo.img" "$BASE/initramfs.img"

PACMAN_Q=$(chroot "$ROOT" pacman -Q)
ENV_ID=$(printf '%s\n%s\n' "$ARCHIVE_DATE" "$PACMAN_Q" | sha256sum | awk '{print $1}')
printf '%s\n' "$ENV_ID" > "$ROOT/etc/waterloo-env-id"

cleanup
MOUNTS=()
rm -f "$BASE/root.squashfs"
mkdir -p "$ROOT/out"
mount --bind "$BASE" "$ROOT/out"
MOUNTS+=("$ROOT/out")
chroot "$ROOT" /usr/bin/mksquashfs / /out/root.squashfs -noappend -comp zstd -b 1M -e out
cleanup
MOUNTS=()

printf '%s\n' "$PACMAN_Q" > "$BASE/pacman-Q.txt"
printf '%s\n' "$ENV_ID" > "$BASE/env_id"
printf 'ARCHIVE_DATE=%s\nBOOTSTRAP=%s\nISOLATE_COMMIT=%s\nENV_ID=%s\n' \
  "$ARCHIVE_DATE" "$BOOTSTRAP" 8f185bb37f3f23e29b33b0c7727c91c13429abe3 "$ENV_ID" > "$BASE/manifest.txt"

chmod 0444 "$BASE/root.squashfs" "$BASE/vmlinuz-linux" "$BASE/initramfs.img" "$BASE/env_id" "$BASE/pacman-Q.txt" "$BASE/manifest.txt"
mkdir -p "$BASE/mounted-root"
mount -t squashfs -o loop,ro "$BASE/root.squashfs" "$BASE/mounted-root"
if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
  chown "$SUDO_UID:$SUDO_GID" "$BASE" "$BASE/root.squashfs" "$BASE/vmlinuz-linux" \
    "$BASE/initramfs.img" "$BASE/env_id" "$BASE/pacman-Q.txt" "$BASE/manifest.txt"
fi
printf 'env_id=%s\n' "$(cat "$BASE/env_id")"
printf 'image=%s/root.squashfs\n' "$BASE"
printf 'kernel=%s/vmlinuz-linux\n' "$BASE"
printf 'initramfs=%s/initramfs.img\n' "$BASE"
