# er_build_tools

## bash helper functions

This repo contains a (hopefully) useful set of utilities to speed up your day to day work! [helper_bash_functions](.helper_bash_functions)

If you have any bash (or python) stuff you use to make your life easier, why not consider adding to [helper_bash_functions](.helper_bash_functions) via PR

This is already in all new arch containers, but if you want it on a host or your own machine, see Initial setup below

### Initial setup
To install these functions on your system, just run:
```bash
curl -fsSL https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/setup-container-shell.sh | bash
source ~/.bashrc
```

Safe to re-run: it will not add a second `source` line if one is already there.

It also repairs ROS1 tab completion if this machine has ROS installed directly
on the host — rosbash otherwise offers no launch files for a workspace under a
dot directory (`/cortex/.catkin_ws`). That rewrite touches root-owned files, so
it asks for sudo at that point and nothing else runs elevated. Machines whose
ROS lives only in a container have nothing to patch and are never prompted. See
[setup-container-shell](docs/setup-container-shell.md).

### Updating helper bash functions

Want the latest version of the bash functions, but you've updated some of the [variables in the file](https://github.com/Extend-Robotics/er_build_tools/blob/5f7bcb50e70efd4b25d483318e3b64501e145e3f/.helper_bash_functions#L8-L9) yourself? Fear not, there's an [updater](https://github.com/Extend-Robotics/er_build_tools/blob/5f7bcb50e70efd4b25d483318e3b64501e145e3f/.helper_bash_functions#L172-L208) inside the bash functions file that will interactively let you preserve these changes should you wish to keep them. Just run:

```bash
update_helper_bash_functions
```


### Using helper bash functions

Below are links to further docs on specific tools, but there are pleanty more undocumented functions [in the helper file](.helper_bash_functions)

- [update_source_repos](docs/update-source-repos.md) - Takes your gh PAT, pulls any source code updates inside your new arch container and then rebuilds your workspace
- [cortex_ws_info](docs/cortex-ws-info.md) - per-repo spec-vs-actual table for the cortex colcon workspaces (ros1/ros2/bridge): local-name | declared branch | as-built SHA | current HEAD | dirty, combining the provenance files baked into the image with live git (`n/a (<why>)` when a file isn't baked). Runs in cortex containers and on dev hosts; replaces the spec-vs-actual view `wstool info` used to provide.
- [usb-camera-healthcheck](docs/usb-camera-healthcheck.md) - report USB link speed + verdict for every UVC camera (sysfs only, no root, curl-bash).
- [jetson-flash-preflight](docs/jetson-flash-preflight.md) - GO / DO NOT FLASH pre-flash check for the AGX Orin 64GB eMMC patch (PCN210100): module detection over USB + flash-tree patch check (curl-bash).
- [er_jetson_flash](docs/er-jetson-flash.md) - one-command AGX Orin QA-cortex flash: verify the JetPack 5.1.2 tree against a canonical manifest (rebuilt from the L4T R35.4.1 tarballs when broken — no SDK Manager, works on Ubuntu 20.04 and 22.04 hosts), preflight gate, flash to NVMe, then post-flash setup (clock, apt over the USB link when offline, nvidia-jetpack, sanity checks).
- [urdf2usd](docs/urdf2usd.md) - URDF --> USD converter (temporarily depricated, see [here](https://github.com/Extend-Robotics/er_interface/pull/156) for more info)

## image build scripts

Scripts run by image builds rather than from an interactive shell.

- [setup-container-shell](docs/setup-container-shell.md) - install the helper functions into an image and repair ROS1 tab completion under a dot-directory workspace (`/cortex/.catkin_ws`). Called once per Dockerfile in `cortex_docker`, after the last apt/rosdep step.
