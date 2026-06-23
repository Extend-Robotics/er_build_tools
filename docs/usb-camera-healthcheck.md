# usb-camera-healthcheck

Reports the negotiated USB link speed and a verdict for every UVC camera on the
host, using sysfs only — no `lsusb`, no `dmesg`, no root, identical on any
Ubuntu/Jetson. Built to diagnose Orbbec depth cameras that silently fall back to
USB2 (see `camera-usb-link-speed-healthcheck-handover.md` for the root cause).

## Run it (curl-bash, no install)

    bash <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/usb-camera-healthcheck.sh)

Watch live while swapping cables until the link is USB3:

    bash <(curl -Ls .../usb-camera-healthcheck.sh) --watch

## Flags

| Flag | Effect |
|------|--------|
| `-w`, `--watch` | Clear-screen redraw every interval until Ctrl-C |
| `-i N`, `--interval N` | Watch refresh seconds (default 5) |
| `-v`, `--verbose` | Also print the full USB device tree |
| `-q`, `--quiet` | No output; verdict via exit code only |
| `-s`, `--strict` | Treat unknown cameras linked below USB3 as failure |
| `-h`, `--help` | Usage |

`--quiet` cannot combine with `--watch` or `--verbose`.

## Verdicts

| Link | Known REQUIRES_USB3 | Known USB2-tolerant | Unknown camera |
|------|---------------------|---------------------|----------------|
| USB3 (≥5000) | OK | OK | OK |
| USB2 (480) | WILL FAIL | OK (reduced spec) | WARN |
| USB1 (12) | WILL FAIL | WARN | WARN |

A `WILL FAIL` camera (e.g. Femto Bolt on USB2) aborts at driver init — fix the
cable and restart the ROS stack.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All cameras OK (or warnings only, without `--strict`) |
| 1 | A REQUIRES_USB3 camera below USB3, or `--strict` and an unknown camera below USB3 |
| 2 | No cameras found |
| 3 | Tool/sysfs error |

## Adding a camera model

Edit the `lookup_model` table in `bin/usb-camera-healthcheck.sh` — one `case`
line per `vid:pid`, mapping to `<name>|REQUIRES_USB3` or `<name>|USB2_TOLERANT`.
Detection itself is generic (USB Video Class), so unknown cameras are still
reported; the table only sharpens the verdict.
