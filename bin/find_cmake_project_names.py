#!/usr/bin/env python3
import sys
import os
import re
import argparse


def _find_closing_bracket(lines, start_index):
    stack = 0
    for i in range(start_index, len(lines)):
        line = lines[i].strip()
        if '(' in line:
            stack += line.count('(')
        if ')' in line:
            stack -= line.count(')')
        if stack == 0:
            return i
    return -1

def _check_for_catkin_top_level(lines):
    for line in lines:
        if 'CATKIN_TOPLEVEL' in line:
            line = line.split(' ')
            for i, elem in enumerate(line):
                if 'CATKIN_TOPLEVEL' in elem:
                    if 'true' in line[i+1].lower() or '1' in line[i+1]:
                        return True
    return False

def _find_project_name_from_cmakelists(lines):
    if _check_for_catkin_top_level(lines):
        return None
    for i, line in enumerate(lines):
        if 'project(' in line.lower() or 'project (' in line.lower():
            closing_index = _find_closing_bracket(lines, i)
            concatenated_lines = ''.join(lines[i:closing_index + 1]).replace('\n', ' ')
            res = re.findall(r"\((.*?)\)", concatenated_lines)
            for x in res[0].split(' '):
                if x != '':
                    res = x
                    break
            return res
    return None


parser = argparse.ArgumentParser(description='Recursively find project name in CMakeLists.txt')
parser.add_argument('path', type=str, help='Path to the directory containing CMakeLists.txt files')
args = parser.parse_args()
root_path = args.path

if not os.path.exists(root_path):
    print(f"Error: The path '{root_path}' does not exist.")
    sys.exit(1)

if not os.path.isdir(root_path):
    print(f"Error: The path '{root_path}' is not a directory.")
    sys.exit(1)

project_names = []
for dirpath, dirnames, filenames in os.walk(root_path):
    if 'CMakeLists.txt' in filenames:
        cmakelists_path = os.path.join(dirpath, 'CMakeLists.txt')
        with open(cmakelists_path, 'r') as file:
            lines = file.readlines()
            project_name = _find_project_name_from_cmakelists(lines)
            if project_name:
                project_names.append(project_name)

if not project_names:
    print("No project names found in CMakeLists.txt files.")
    exit(1)

for name in project_names[:-1]:
    print(f"{name}", end=' ')
print(f"{project_names[-1]}", end='')



## EXAMPLE USAGE: python3.8 <(curl -Ls https://raw.githubusercontent.com/Extend-Robotics/er_build_tools/refs/heads/main/bin/find_cmake_project_names.py) /PATH/TO/SEARCH
