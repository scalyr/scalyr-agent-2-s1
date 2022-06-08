# Copyright 2014-2022 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This is a template of the script that can be used in a BuildStep (see agent_build/tools/builder.py).
# PLEASE NOTE. To achieve valid caching of the build step, keep that script as standalone as possible.
#   If there are any dependencies, imports or files which are used by this script, then also add them
#   to the `TRACKED_FILE_GLOBS` attribute of the step class.

import pathlib as pl
import os
import sys

# Here are some environment variables, which are pre-defined for all steps:
# Path to the source root of the project.
SOURCE_ROOT = pl.Path(os.environ["SOURCE_ROOT"])
# Path where the step has to save its results.
STEP_OUTPUT_PATH = pl.Path(os.environ["STEP_OUTPUT_PATH"])

# If step has another steps that it depends on, then it can access their output directories from command line arguments.
# The order matches the order which is defined in the step class.
# Uncomment and edit this, if needed.
DEP_STEP1_OUTPUT, = sys.argv[1:]

cached_result_path = STEP_OUTPUT_PATH / "result.txt"
input_value = os.environ["INPUT"]

base_result_file_path = pl.Path(os.environ["BASE_RESULT_FILE_PATH"])
dependency_step_result_filepath = pl.Path(DEP_STEP1_OUTPUT) / "result.txt"


result_content = f"{base_result_file_path.read_text()}\n"
result_content += f"{dependency_step_result_filepath.read_text()}"
result_content += f"{input_value}\npython"

in_docker_file_path = pl.Path("/docker")
if in_docker_file_path.is_file():
    result_content = f"{result_content}\ndocker"
cached_result_path.write_text(result_content)
