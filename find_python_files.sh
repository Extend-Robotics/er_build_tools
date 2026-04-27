#!/bin/sh
#
# find_python_files.sh
#
# Print Python source files at-or-below the current directory, one per
# line, sorted and deduplicated. Shared by the local linter helper and
# the CI workflow so both see the same set of files.
#
# DETECTION
#   A file counts as Python if EITHER condition holds:
#     - filename ends in .py or .pyi    (library modules, normal scripts)
#     - `file -bi` reports a mime type containing "python" (driven
#       internally by the file having a Python shebang on line 1, which
#       is also the only way the kernel recognises it as Python — e.g.
#       ROS nodes in scripts/ exec'd by name with no extension).
#       libmagic emits either text/x-python or text/x-script.python
#       depending on version, hence the substring match rather than an
#       exact one (see Extend-Robotics/er_build_tools#37).
#   Both rules are needed because they catch disjoint cases:
#     - Library code under src/ has .py but no shebang (libraries are
#       not meant to be directly executable), so mime-only would miss
#       every src/ module — `file` reports them as text/plain since
#       they have no shebang and Python has no magic number.
#     - ROS nodes named like `scripts/my_node` have a shebang but no
#       .py extension, so extension-only would miss them.
#   The earlier `find_python_files_here` shipped only the mime check
#   and so silently skipped every .py library file under src/. The
#   extension check fixes that; the mime check covers the no-extension
#   case.
#
# SCOPE
#   Two modes, picked automatically:
#
#     git mode (cwd inside a git work tree, git installed):
#       - uses `git ls-files` for enumeration
#       - .gitignore is respected for free — no build/, devel/, .venv/,
#         __pycache__/, generated stubs, project-specific ignores
#       - .git/ is never traversed
#       - tracked files only; untracked-but-intended Python is NOT
#         returned. Switch to `git ls-files --others --exclude-standard
#         --cached` if you need the staged-or-tracked set instead.
#
#     find mode (non-git dirs, or git missing/broken):
#       - walks the filesystem from cwd
#       - prunes the dirs listed in $EXCLUDE_DIRS below. The default
#         list is intentionally minimal (only universally non-source
#         dirs). Project-specific build/output/venv dirs (build/,
#         devel/, .venv/, output/, .cache/, ...) are NOT excluded by
#         default — operate in git mode to get .gitignore for free, or
#         override EXCLUDE_DIRS for the run.
#
# OUTPUT
#   - Paths relative to cwd, no `./` prefix, one per line, sorted.
#   - No output and exit 0 when there are no Python files.
#
# NON-GOALS
#   - .pyx (Cython) is intentionally not detected; pylint cannot lint it.
#   - No content / style / blacklist filtering. Callers layer that on top
#     (the CI workflow's BLACKLIST input does this).
#

# Directories pruned in find mode. Kept minimal — only dirs that are
# never source under any convention. Project-specific dirs (build/,
# devel/, .venv/, output/, ...) should be added per-project, or use
# git mode (where .gitignore handles them for free).
# Override at invocation:  EXCLUDE_DIRS='__pycache__ build' ./find_python_files.sh
EXCLUDE_DIRS="${EXCLUDE_DIRS:-__pycache__}"

# Print $1 to stdout iff it's a Python source file.
#
# `file` reports Python with either `text/x-python` or
# `text/x-script.python` depending on libmagic version (see
# Extend-Robotics/er_build_tools#37), so we grep for the substring
# `python` in the mime line. `-b` strips the filename from the output
# so a non-Python file whose name contains "python" doesn't slip in.
emit_if_python() {
    case "$1" in
        *.py|*.pyi) printf '%s\n' "$1"; return 0 ;;
    esac
    if [ -f "$1" ] && file -bi "$1" 2>/dev/null | grep -q python; then
        printf '%s\n' "$1"
    fi
}

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files | while IFS= read -r f; do
        emit_if_python "$f"
    done | sort -u
else
    # Build "! -path '*/X/*'" args from EXCLUDE_DIRS without eval.
    set --
    for dir in $EXCLUDE_DIRS; do
        set -- "$@" '!' -path "*/$dir/*"
    done
    find . "$@" -type f | while IFS= read -r f; do
        emit_if_python "${f#./}"
    done | sort -u
fi
