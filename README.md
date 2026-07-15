# er_build_tools

## bash helper functions

This repo contains a (hopefully) useful set of utilities to speed up your day to day work! [helper_bash_functions](.helper_bash_functions)

If you have any bash (or python) stuff you use to make your life easier, why not consider adding to [helper_bash_functions](.helper_bash_functions) via PR

### Initial setup
To install these functions on your system, just run:
```bash
wget -O ~/.helper_bash_functions https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/.helper_bash_functions
echo "source ~/.helper_bash_functions" >> ~/.bashrc && source ~/.bashrc
```

### Updating helper bash functions

Want the latest version of the bash functions, but you've updated some of the [variables in the file](https://github.com/Extend-Robotics/er_build_tools/blob/5f7bcb50e70efd4b25d483318e3b64501e145e3f/.helper_bash_functions#L8-L9) yourself? Fear not, there's an [updater](https://github.com/Extend-Robotics/er_build_tools/blob/5f7bcb50e70efd4b25d483318e3b64501e145e3f/.helper_bash_functions#L172-L208) inside the bash functions file that will interactively let you preserve these changes should you wish to keep them. Just run:

```bash
update_helper_bash_functions
```


### Tools

- [update_source_repos](docs/update-source-repos.md) - Takes your gh PAT, pulls any source code updates inside your new arch container and then rebuilds your workspace
- [usb-camera-healthcheck](docs/usb-camera-healthcheck.md) - report USB link speed + verdict for every UVC camera (sysfs only, no root, curl-bash).
- [jetson-flash-preflight](docs/jetson-flash-preflight.md) - GO / DO NOT FLASH pre-flash check for the AGX Orin 64GB eMMC patch (PCN210100): module detection over USB + flash-tree patch check (curl-bash).
- [urdf2usd](docs/urdf2usd.md) - URDF → USD converter (temporarily depricated, see [here](https://github.com/Extend-Robotics/er_interface/pull/156) for more info)
