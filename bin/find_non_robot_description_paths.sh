#!/bin/bash

find_cmake_for_models() {
    local start_dir="${1:-.}"

    # Find all model files recursively
    find "$start_dir" -type f \( \
        -iname "*.stl" -o -iname "*.dae" -o -iname "*.urdf" -o -iname "*.xacro" \
    \) -print0 |
    xargs -0 -n1 -P "$(nproc 2>/dev/null || echo 4)" bash -c '
        file="$0"
        dir=$(dirname "$file")
        while [ "$dir" != "/" ]; do
            if [ -f "$dir/CMakeLists.txt" ]; then
                realpath "$dir/CMakeLists.txt"
                break
            fi
            dir=$(dirname "$dir")
        done
    ' |
    sort -u
}



SEARCH_PATH="${1:-.}"
CONTAINS_MODELS=$(find_cmake_for_models ${SEARCH_PATH})
ALL=$(find ${SEARCH_PATH} -name "*CMakeLists.txt")

NON_MODEL_PATHS=$(comm -13 <(find_cmake_for_models ${SEARCH_PATH} | sort) <(find ${SEARCH_PATH} -name "*CMakeLists.txt" | sort))

#for NON_MODEL_PATH in $(echo $NON_MODEL_PATHS ); do PATH_TO_REMOVE=$(dirname ${NON_MODEL_PATH}); echo "Removing ${PATH_TO_REMOVE}"; rm -r ${PATH_TO_REMOVE}; done
for NON_MODEL_PATH in $(echo $NON_MODEL_PATHS ); do PATH_TO_REMOVE=$(dirname ${NON_MODEL_PATH}); echo "${PATH_TO_REMOVE}"; done


## EXAMPLE USAGE: python3.8 <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/find_non_robot_description_paths.sh) /PATH/TO/SEARCH
