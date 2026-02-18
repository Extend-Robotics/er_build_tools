#!/usr/bin/env python3
"""Merge user-customised variables when updating .helper_bash_functions.

Reads old and new versions of the file, extracts top-level variable assignments,
and applies these rules:
  - Variable only in old file (user-added): preserve it
  - Same value in both: keep new (no-op)
  - Different value: ask the user which to keep

Writes the merged result to the new file path.

Usage: python3 merge_helper_vars.py <old_file> <new_file>
"""
import re
import sys

SKIP_VARS = {"Red", "Green", "Yellow", "Color_Off"}
VAR_PATTERN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)')
REFERENCES_OTHER_VAR = re.compile(r'\$\{')
FUNCTION_OR_SECTION = re.compile(r'^[a-zA-Z_]+\(\)|^# [A-Z]')


def extract_top_level_vars(filepath):
    """Extract VAR=value lines from the top of the file, before functions start."""
    variables = {}
    with open(filepath, encoding="utf-8") as file_handle:
        for line in file_handle:
            stripped = line.rstrip('\n')
            if FUNCTION_OR_SECTION.match(stripped):
                break
            match = VAR_PATTERN.match(stripped)
            if not match:
                continue
            var_name = match.group(1)
            if var_name in SKIP_VARS:
                continue
            if REFERENCES_OTHER_VAR.search(match.group(2)):
                continue
            variables[var_name] = match.group(2)
    return variables


def ask_user(var_name, old_value, new_value):
    """Ask user which value to keep. Returns the chosen value."""
    print(f"\n\033[0;33m{var_name} has changed:\033[0m")
    print(f"  Current: {old_value}")
    print(f"  Updated: {new_value}")
    response = input("  Keep current value? [Y/n] ").strip().lower()
    if response in ("n", "no"):
        print("  \033[0;32mUsing updated value\033[0m")
        return new_value
    print("  \033[0;32mKeeping current value\033[0m")
    return old_value


def apply_var_to_file(filepath, var_name, value):
    """Set a variable in the file, replacing if present or inserting after Color_Off."""
    with open(filepath, encoding="utf-8") as file_handle:
        lines = file_handle.readlines()

    new_line = f"{var_name}={value}\n"
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{var_name}="):
            lines[i] = new_line
            replaced = True
            break

    if not replaced:
        for i, line in enumerate(lines):
            if line.startswith("Color_Off="):
                lines.insert(i + 1, new_line)
                break

    with open(filepath, "w", encoding="utf-8") as file_handle:
        file_handle.writelines(lines)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <old_file> <new_file>")
        sys.exit(1)

    old_file, new_file = sys.argv[1], sys.argv[2]
    old_vars = extract_top_level_vars(old_file)
    new_vars = extract_top_level_vars(new_file)

    vars_to_apply = {}

    for var_name, old_value in old_vars.items():
        if var_name not in new_vars:
            print(f"\033[0;32mPreserving\033[0m {var_name}={old_value} (not in updated script)")
            vars_to_apply[var_name] = old_value
        elif old_value != new_vars[var_name]:
            chosen = ask_user(var_name, old_value, new_vars[var_name])
            if chosen == old_value:
                vars_to_apply[var_name] = old_value
        # else: same value, nothing to do

    for var_name, value in vars_to_apply.items():
        apply_var_to_file(new_file, var_name, value)


if __name__ == "__main__":
    main()
