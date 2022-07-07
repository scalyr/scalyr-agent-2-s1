#!/bin/bash
# Copyright 2014-2020 Scalyr Inc.
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

# This script check for installed python interpreter with opropriate version.
# In some distributions, there is no 'python' command even if python interpreter is installed.
# In this cases script have to exit with an error.
# This is important because all agent scripts rely on '/usr/bin/env python' command.


# Always remove the .pyc files and __pycache__ directories.  This covers problems for old packages that didn't have the remove in the
# preuninstall.sh script.
if [ -d /usr/share/scalyr-agent-2 ]; then
  find /usr/share/scalyr-agent-2 -type f -name '*.py[co]' -delete -o -type d -name __pycache__ -exec rm -r {} \;
fi

exit 0
