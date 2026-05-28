# urdf2usd

Run [`urdf-usd-converter`](https://github.com/newton-physics/urdf-usd-converter)
on a glibc-2.31 host (Ubuntu 20.04 / ROS Noetic) by sandboxing it under
[`bwrap`](https://github.com/containers/bubblewrap) inside a pre-built
glibc-2.36 rootfs.

The converter wheel ships only `manylinux_2_35` binaries (it links against
`GLIBC_2.34`/`GLIBC_2.35` symbols), so a native install fails on Ubuntu 20.04.
This package gives you a `urdf2usd` CLI on PATH that works the same way on
20.04 today and on 22.04+ tomorrow.

## Install on a running host (one-liner)

Installs the latest published `urdf2usd-v*` release:

```bash
curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/main/bin/install-urdf2usd.sh | sudo bash
```

To pin a specific converter version:

```bash
curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/main/bin/install-urdf2usd.sh \
    | sudo bash -s -- --version 0.1.3
```

The installer apt-installs `bubblewrap`, downloads the rootfs tarball from
the matching GitHub Release, extracts it to `/opt/urdf-usd-converter-rootfs/`,
and drops the `urdf2usd` wrapper at `/usr/local/bin/urdf2usd`.

## Use

```bash
urdf2usd /path/to/robot.urdf /path/to/output_usd
urdf2usd /path/to/robot.urdf /path/to/output_usd --package my_pkg=/path/to/my_pkg
urdf2usd --help
```

The wrapper bind-mounts `/cortex`, `/home`, `/tmp`, `/var/tmp`, `/mnt`,
`/media` into the sandbox, so absolute paths to inputs and output dirs under
those roots Just Work. For inputs elsewhere, either edit the bind list at the
top of `bin/urdf2usd` or symlink into one of the bound roots.

## Bake into a Docker image at build time (no install step at runtime)

If you're building an image (e.g. a Noetic robot image) and want `urdf2usd`
baked in, use a multi-stage build to copy the converter rootfs in directly.
This avoids the runtime download entirely.

```dockerfile
# Stage 1: build the converter rootfs
FROM python:3.12-slim AS urdf_usd_rootfs
ARG CONVERTER_VERSION=0.1.3
RUN pip install --no-cache-dir "urdf-usd-converter==${CONVERTER_VERSION}" \
    && mkdir -p /cortex

# Stage 2: your image
FROM <your-base-image>

COPY --from=urdf_usd_rootfs / /opt/urdf-usd-converter-rootfs/
RUN apt-get update && apt-get install -y bubblewrap && rm -rf /var/lib/apt/lists/*
ADD https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/main/bin/urdf2usd /usr/local/bin/urdf2usd
RUN chmod +x /usr/local/bin/urdf2usd
```

`urdf2usd` is then on PATH in the resulting image with no runtime download,
no docker, no daemon, and no separate install step.

## Cutting a new release

The rootfs tarball is built and uploaded as a GitHub Release asset by:

```bash
bin/build-and-release-urdf2usd.sh                     # latest urdf-usd-converter on PyPI
bin/build-and-release-urdf2usd.sh 0.1.3               # pinned converter version
```

That script needs `docker`, `gh` (authenticated), `tar`, `gzip`, `curl`, and
`python3` on the machine running it. It tags the release
`urdf2usd-v<converter-version>` and uploads
`urdf-usd-converter-rootfs-<version>.tar.gz` as an asset, producing a stable
public URL of the form:

```
https://github.com/Extend-Robotics/er_build_tools/releases/download/urdf2usd-v<version>/urdf-usd-converter-rootfs-<version>.tar.gz
```

The installer auto-discovers the latest release via the GitHub API, so no
follow-up edits are needed after a new release is cut.

## Why bwrap (not chroot, not docker)?

- **`chroot`** needs root, doesn't isolate processes/IPC/network, and won't
  set up a `/proc` or `/dev` for you. Bwrap is a thin layer on top of Linux
  user namespaces that handles all of that and runs unprivileged.
- **`docker run`** needs a daemon at runtime. The whole reason this exists
  is that the consumer container (Noetic) doesn't ship docker.

## Migration to glibc 2.35+ (Ubuntu 22.04+, Debian bookworm+)

On a host where the converter installs natively, you can drop bwrap entirely:

```bash
pip install urdf-usd-converter
```

This installs the `urdf_usd_converter` binary on PATH. At that point you can
remove `/opt/urdf-usd-converter-rootfs/` and `/usr/local/bin/urdf2usd` (or
keep the wrapper as a stable alias and change its body to `exec urdf_usd_converter "$@"`).
Caller code that invokes `urdf2usd …` keeps working unchanged.
