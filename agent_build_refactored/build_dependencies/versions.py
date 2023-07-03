# Copyright 2014-2023 Scalyr Inc.
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

"""
This module defines version for Python interpreter and other dependencies.
"""

PYTHON_VERSION = "3.11.2"

# Versions of OpenSSL libraries to build for Python.
OPENSSL_1_VERSION = "1.1.1s"
OPENSSL_3_VERSION = "3.0.7"

# Integer (hex) representation of the OpenSSL version.
EMBEDDED_OPENSSL_VERSION_NUMBER = 0x1010113F

# Version of Rust to use in order to build some of agent's requirements, e.g. orjson.
RUST_VERSION = "1.63.0"

EMBEDDED_PYTHON_PIP_VERSION = "23.0"