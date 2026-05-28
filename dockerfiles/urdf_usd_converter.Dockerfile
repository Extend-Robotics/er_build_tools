# Builds a minimal rootfs containing urdf-usd-converter for use with the
# bwrap-based urdf2usd wrapper. The filesystem of a container built from this
# image is exported as a tarball by bin/build-and-release-urdf2usd.sh.
#
# This image is not intended to be run directly; the consumer extracts its
# filesystem into /opt/urdf-usd-converter-rootfs/ and invokes the converter
# via bwrap, so docker is only needed at build/publish time.

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG CONVERTER_VERSION
RUN test -n "${CONVERTER_VERSION}" || { echo "CONVERTER_VERSION build-arg required" >&2; exit 1; } && \
    pip install --no-cache-dir "urdf-usd-converter==${CONVERTER_VERSION}"

# bwrap --bind requires mountpoints to exist in the target rootfs.
RUN mkdir -p /cortex /workspace
