# Copyright 2014 Scalyr Inc.
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
# ------------------------------------------------------------------------
#
# author: Steven Czerwinski <czerwin@scalyr.com>

from __future__ import unicode_literals
from __future__ import absolute_import

__author__ = "czerwin@scalyr.com"

from sys import platform as _platform

if False:
    from typing import List

from scalyr_agent.json_lib import JsonObject
from scalyr_agent.platform_posix import PosixPlatformController
from scalyr_agent.configuration import Configuration
from scalyr_agent.copying_manager.copying_manager import (
    WORKER_SESSION_PROCESS_MONITOR_ID_PREFIX,
)


class LinuxPlatformController(PosixPlatformController):
    """The platform controller for Linux platforms.

    This is based on the general Posix platform but also adds in Linux-specific monitors to run.
    """

    def __init__(self, stdin="/dev/null", stdout="/dev/null", stderr="/dev/null"):
        """Initializes the POSIX platform instance."""
        PosixPlatformController.__init__(
            self, stdin=stdin, stdout=stdout, stderr=stderr
        )

    def can_handle_current_platform(self):
        """Returns true if this platform object can handle the server this process is running on.

        @return:  True if this platform instance can handle the current server.
        @rtype: bool
        """
        return _platform.lower().startswith("linux")

    def get_default_monitors(self, config):  # type: (Configuration) -> List
        """Returns the default monitors to use for this platform.

        This method should return a list of dicts containing monitor configuration options just as you would specify
        them in the configuration file.  The list may be empty.

        @param config The configuration object to use.
        @type config configuration.Configuration

        @return: The default monitors
        @rtype: list<dict>
        """
        result = []

        if config.implicit_metric_monitor:
            result.append(
                JsonObject(
                    module="scalyr_agent.builtin_monitors.linux_system_metrics",
                )
            )

        if config.implicit_agent_process_metrics_monitor:
            result.append(
                JsonObject(
                    module="scalyr_agent.builtin_monitors.linux_process_metrics",
                    pid="$$",
                    id="agent",
                )
            )
            # if multi-process workers are enabled and worker session processes monitoring is enabled,
            # then create linux metrics monitor for each worker process.
            if (
                config.use_multiprocess_workers
                and config.enable_worker_session_process_metrics_gather
            ):
                for worker_config in config.worker_configs:
                    for worker_session_id in config.get_session_ids_of_the_worker(
                        worker_config
                    ):
                        result.append(
                            JsonObject(
                                module="scalyr_agent.builtin_monitors.linux_process_metrics",
                                # the copying manager start after the declaration of the managers,
                                # so we can not put the real PID but just mark that it will be set later.
                                pid="$$TBD",
                                id="{0}{1}".format(
                                    WORKER_SESSION_PROCESS_MONITOR_ID_PREFIX,
                                    worker_session_id,
                                ),
                            )
                        )
        return result
