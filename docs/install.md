# Install

You need a **PF1007L PL5 / Allwinner A33**, a spare microSD card (16 GB+),
Python 3.11+, Git and [mise](https://mise.jdx.dev/).

**Mac:** Apple Silicon, macOS 14+, Apple Command Line Tools (`xcode-select --install`).

**Linux:** kernel 5.8+ with Btrfs; util-linux, btrfs-progs, dosfstools, libarchive
(`bsdtar`), gpgv, GNU tar, kmod, zstd and coreutils. Non-ARMv7 hosts also need
ARM32 binfmt execution (`qemu-user-static`, flag `F`).

## Write the card

Insert the card into your computer's reader. In the repository:

```sh
mise install
python3 image/install.py list
python3 image/install.py install --disk DISK --capacity BYTES --erase
```

Use the disk identifier and exact capacity from `list`. **The selected card is erased.**
Run as your normal user; administrator access is requested when needed.

Choose an existing public SSH key, paste one, set a password, or leave SSH off.
Use arrow keys and Enter; `--plain` uses numbered prompts. To skip the picker,
pass `--ssh-key ~/.ssh/id_ed25519.pub`, `--ssh-password`, or `--no-ssh`.
Password access needs OpenSSL 3 (`brew install openssl@3` on Mac).

Rerun the command to resume an interrupted install.

## First boot

Power off, insert the card and reconnect power. Set the name, timezone and Wi-Fi
on the touchscreen. Progress is saved; storage expands automatically.
Change settings later in **Home → Settings → Setup**.

For USB serial setup or recovery, connect the frame's USB device port and open
`/dev/cu.usbmodem…` (Mac) or `/dev/ttyACM…` (Linux):

```sh
screen SERIAL_DEVICE 115200
```

Run `fascia setup`. Serial and touchscreen setup share saved progress.
Setup shows the Wi-Fi IP and hostname, such as `fascia-m7k3p.local`.
SSH login: `fascia`.
