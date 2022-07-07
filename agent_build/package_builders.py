# Copyright 2014-2021 Scalyr Inc.
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
This module defines all possible packages of the Scalyr Agent and how they can be built.
"""
import argparse
import json
import pathlib as pl
import shlex
import tarfile
import abc
import shutil
import time
import sys
import stat
import os
import re
import platform
import subprocess
import logging
from typing import Union, Optional, List, Dict, Type

from agent_build.tools import constants
from agent_build.tools.environment_deployments import deployments
from agent_build.tools import build_in_docker
from agent_build.tools import common
from agent_build.tools.constants import Architecture, PackageType
from agent_build.tools.environment_deployments.deployments import ShellScriptDeploymentStep, DeploymentStep, CacheableBuilder, BuilderInput
from agent_build.prepare_agent_filesystem import build_linux_lfs_agent_files, get_install_info, create_change_logs
from agent_build.tools.constants import SOURCE_ROOT


__PARENT_DIR__ = pl.Path(__file__).absolute().parent
__SOURCE_ROOT__ = __PARENT_DIR__.parent

_AGENT_BUILD_PATH = __SOURCE_ROOT__ / "agent_build"


def cat_files(file_paths, destination, convert_newlines=False):
    """Concatenates the contents of the specified files and writes it to a new file at destination.

    @param file_paths: A list of paths for the files that should be read. The concatenating will be done in the same
        order as the list.
    @param destination: The path of the file to write the contents to.
    @param convert_newlines: If True, the final file will use Windows newlines (i.e., CR LF).
    """
    with pl.Path(destination).open("w") as dest_fp:
        for file_path in file_paths:
            with pl.Path(file_path).open("r") as in_fp:
                for line in in_fp:
                    if convert_newlines:
                        line.replace("\n", "\r\n")
                    dest_fp.write(line)


def recursively_delete_files_by_name(
    dir_path: Union[str, pl.Path], *file_names: Union[str, pl.Path]
):
    """Deletes any files that are in the current working directory or any of its children whose file names
    match the specified regular expressions.

    This will recursively examine all children of the current working directory.

    @param file_names: A variable number of strings containing regular expressions that should match the file names of
        the files that should be deleted.
    """
    # Compile the strings into actual regular expression match objects.
    matchers = []
    for file_name in file_names:
        matchers.append(re.compile(str(file_name)))

    # Walk down the current directory.
    for root, dirs, files in os.walk(dir_path.absolute()):
        # See if any of the files at this level match any of the matchers.
        for file_path in files:
            for matcher in matchers:
                if matcher.match(file_path):
                    # Delete it if it did match.
                    os.unlink(os.path.join(root, file_path))
                    break


def recursively_delete_dirs_by_name(root_dir: Union[str, pl.Path], *dir_names: str):
    """
    Deletes any directories that are in the current working directory or any of its children whose file names
    match the specified regular expressions.

    This will recursively examine all children of the current working directory.

    If a directory is found that needs to be deleted, all of it and its children are deleted.

    @param dir_names: A variable number of strings containing regular expressions that should match the file names of
        the directories that should be deleted.
    """

    # Compile the strings into actual regular expression match objects.
    matchers = []
    for dir_name in dir_names:
        matchers.append(re.compile(dir_name))

    # Walk down the file tree, top down, allowing us to prune directories as we go.
    for root, dirs, files in os.walk(root_dir):
        # Examine all directories at this level, see if any get a match
        for dir_path in dirs:
            for matcher in matchers:
                if matcher.match(dir_path):
                    shutil.rmtree(os.path.join(root, dir_path))
                    # Also, remove it from dirs so that we don't try to walk down it.
                    dirs.remove(dir_path)
                    break


class CacheableStepsRunner:
    NAME: str
    CACHEABLE_DEPLOYMENT_STEPS: List[DeploymentStep] = []


class PackageBuilder(abc.ABC):
    """
        Base abstraction for all Scalyr agent package builders. it can perform build of the package directly on the
    current system or inside docker.
        It also uses ':py:module:`agent_build.tools.environment_deployments` features to define and deploy its build
        environment in order to be able to perform the actual build.
    """

    NAME: str

    # Type of the package to build.
    PACKAGE_TYPE: constants.PackageType = None

    # Add agent source code as a bundled frozen binary if True, or
    # add the source code as it is.
    USE_FROZEN_BINARIES: bool = True

    # Specify the name of the frozen binary, if it is used.
    FROZEN_BINARY_FILE_NAME = "scalyr-agent-2"

    # The type of the installation. For more info, see the 'InstallType' in the scalyr_agent/__scalyr__.py
    INSTALL_TYPE: str

    # Map package-specific architecture names to the architecture names that are used in build.
    PACKAGE_FILENAME_ARCHITECTURE_NAMES: Dict[constants.Architecture, str] = {}

    # The format string for the glob that has to match result package filename.
    # For now, the format string accepts:
    #   {arch}: architecture of the package.
    # See more in the "filename_glob" property of the class.
    RESULT_PACKAGE_FILENAME_GLOB: str

    # Monitors that are no included to to the build. Makes effect only with frozen binaries.
    EXCLUDED_MONITORS = []

    def __init__(
        self,
        architecture: constants.Architecture = constants.Architecture.UNKNOWN,
        base_docker_image: str = None,
        deployment_steps: List[deployments.DeploymentStep] = None,
        variant: str = None,
        no_versioned_file_name: bool = False,
    ):
        """
        :param architecture: Architecture of the package.
        :param variant: Adds the specified string into the package's iteration name. This may be None if no additional
        tweak to the name is required. This is used to produce different packages even for the same package type (such
        as 'rpm').
        :param no_versioned_file_name:  True if the version number should not be embedded in the artifact's file name.
        """

        self.architecture = architecture

        # The path where the build output will be stored.
        self._build_output_path: Optional[pl.Path] = (
            constants.PACKAGE_BUILDER_OUTPUT / self.name
        )
        # Folder with intermediate and temporary results of the build.
        self._intermediate_results_path = (
            self._build_output_path / "intermediate_results"
        )
        # The path of the folder where all files of the package will be stored.
        # Also may be helpful for the debug purposes.
        self._package_root_path = self._build_output_path / "package_root"

        self._variant = variant
        self._no_versioned_file_name = no_versioned_file_name

        self.base_docker_image = base_docker_image

        self.output_path = constants.DEPLOYMENT_OUTPUT / type(self).NAME

        self.deployment_steps = deployment_steps

    @property
    def name(self) -> str:
        """
        Unique name of the package builder. It considers the architecture of the package.
        """

        name = type(self).PACKAGE_TYPE.value

        # Omit architecture if unknown.
        if self.architecture != constants.Architecture.UNKNOWN:
            name = f"{name}_{self.architecture.value}"

        return name

    @property
    def filename_glob(self) -> str:
        """
        Final glob that has to match result package filename.
        """

        # Get appropriate glob format string and apply the appropriate architecture.
        package_specific_arch_name = type(self).PACKAGE_FILENAME_ARCHITECTURE_NAMES.get(
            self.architecture, ""
        )
        return type(self).RESULT_PACKAGE_FILENAME_GLOB.format(
            arch=package_specific_arch_name
        )

    def build(self, locally: bool = False):
        """
        The function where the actual build of the package happens.
        :param locally: Force builder to build the package on the current system, even if meant to be done inside
            docker. This is needed to avoid loop when it is already inside the docker.
        """



        # Build right here.
        if locally:
            self._build()
            return

        # Build in docker.

        # To perform the build in docker we have to run the build_package.py script once more but in docker.
        build_package_script_path = pl.Path("/scalyr-agent-2/build_package_new.py")

        command_args = [
            "python3",
            str(build_package_script_path),
            self.NAME,
            "--output-dir",
            "/tmp/build",
            # Do not forget to specify this flag to avoid infinite docker build recursion.
            "--locally",
        ]

        command = shlex.join(command_args)  # pylint: disable=no-member

        # Run the docker build inside the result image of the deployment.
        base_image_name = self.result_image_name.lower()

        build_in_docker.build_stage(
            command=command,
            stage_name="build",
            architecture=self.architecture,
            image_name=f"agent-builder-{self.NAME}-{base_image_name}".lower(),
            base_image_name=base_image_name,
            output_path_mappings={self._build_output_path: pl.Path("/tmp/build")},
        )

    @property
    def _build_info(self) -> Dict:
        """Returns a dict containing the package build info."""

        build_info = {}

        original_dir = os.getcwd()

        try:
            # We need to execute the git command in the source root.
            os.chdir(__SOURCE_ROOT__)
            # Add in the e-mail address of the user building it.
            try:
                packager_email = (
                    subprocess.check_output("git config user.email", shell=True)
                    .decode()
                    .strip()
                )
            except subprocess.CalledProcessError:
                packager_email = "unknown"

            build_info["packaged_by"] = packager_email

            # Determine the last commit from the log.
            commit_id = (
                subprocess.check_output(
                    "git log --summary -1 | head -n 1 | cut -d ' ' -f 2", shell=True
                )
                .decode()
                .strip()
            )

            build_info["latest_commit"] = commit_id

            # Include the branch just for safety sake.
            branch = (
                subprocess.check_output("git branch | cut -d ' ' -f 2", shell=True)
                .decode()
                .strip()
            )
            build_info["from_branch"] = branch

            # Add a timestamp.
            build_info["build_time"] = time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime()
            )

            return build_info
        finally:
            os.chdir(original_dir)

    @property
    def _install_info(self) -> Dict:
        """
        Get dict with installation info.
        """
        return {"build_info": self._build_info, "install_type": type(self).INSTALL_TYPE}

    @property
    def _install_info_str(self) -> str:
        """
        Get json serialized string with installation info.
        """
        return json.dumps(self._install_info, indent=4, sort_keys=True)

    @staticmethod
    def _add_config(
        config_source_path: Union[str, pl.Path], output_path: Union[str, pl.Path]
    ):
        """
        Copy config folder from the specified path to the target path.
        """
        config_source_path = pl.Path(config_source_path)
        output_path = pl.Path(output_path)
        # Copy config
        shutil.copytree(config_source_path, output_path)

        # Make sure config file has 640 permissions
        config_file_path = output_path / "agent.json"
        config_file_path.chmod(int("640", 8))

        # Make sure there is an agent.d directory regardless of the config directory we used.
        agent_d_path = output_path / "agent.d"
        agent_d_path.mkdir(exist_ok=True)
        # NOTE: We in intentionally set this permission bit for agent.d directory to make sure it's not
        # readable by others.
        agent_d_path.chmod(int("741", 8))

        # Also iterate through all files in the agent.d and set appropriate permissions.
        for child_path in agent_d_path.iterdir():
            if child_path.is_file():
                child_path.chmod(int("640", 8))

    @staticmethod
    def _add_certs(
        path: Union[str, pl.Path], intermediate_certs=True, copy_other_certs=True
    ):
        """
        Create needed certificates files in the specified path.
        """

        path = pl.Path(path)
        path.mkdir(parents=True)
        source_certs_path = __SOURCE_ROOT__ / "certs"

        cat_files(source_certs_path.glob("*_root.pem"), path / "ca_certs.crt")

        if intermediate_certs:
            cat_files(
                source_certs_path.glob("*_intermediate.pem"),
                path / "intermediate_certs.pem",
            )
        if copy_other_certs:
            for cert_file in source_certs_path.glob("*.pem"):
                shutil.copy(cert_file, path / cert_file.name)

    @property
    def _package_version(self) -> str:
        """The version of the agent"""
        return pl.Path(__SOURCE_ROOT__, "VERSION").read_text().strip()

    def _build_frozen_binary(
        self,
        output_path: Union[str, pl.Path],
    ):
        """
        Build the frozen binary using the PyInstaller library.
        """
        output_path = pl.Path(output_path)

        # Create the special folder in the package output directory where the Pyinstaller's output will be stored.
        # That may be useful during the debugging.
        pyinstaller_output = self._intermediate_results_path / "frozen_binary"
        pyinstaller_output.mkdir(parents=True, exist_ok=True)

        scalyr_agent_package_path = __SOURCE_ROOT__ / "scalyr_agent"

        # Create package info file. It will be read by agent in order to determine the package type and install root.
        # See '__determine_install_root_and_type' function in scalyr_agent/__scalyr__.py file.
        install_info_file = self._intermediate_results_path / "install_info.json"

        install_info_file.write_text(self._install_info_str)

        # Add this package_info file in the 'scalyr_agent' package directory, near the __scalyr__.py file.
        add_data = {str(install_info_file): "scalyr_agent"}

        # Add monitor modules as hidden imports, since they are not directly imported in the agent's code.
        all_builtin_monitor_module_names = [
            mod_path.stem
            for mod_path in pl.Path(
                __SOURCE_ROOT__, "scalyr_agent", "builtin_monitors"
            ).glob("*.py")
            if mod_path.stem != "__init__"
        ]

        hidden_imports = []

        # We also have to filter platform dependent monitors.
        for monitor_name in all_builtin_monitor_module_names:
            if monitor_name in type(self).EXCLUDED_MONITORS:
                continue
            hidden_imports.append(f"scalyr_agent.builtin_monitors.{monitor_name}")

        # Add packages to frozen binary paths.
        paths_to_include = [
            str(scalyr_agent_package_path),
            str(scalyr_agent_package_path / "builtin_monitors"),
        ]

        # Add platform specific things.
        if platform.system().lower().startswith("linux"):
            tcollectors_path = pl.Path(
                __SOURCE_ROOT__,
                "scalyr_agent",
                "third_party",
                "tcollector",
                "collectors",
            )
            add_data.update(
                {tcollectors_path: tcollectors_path.relative_to(__SOURCE_ROOT__)}
            )

        # Create --add-data options from previously added files.
        add_data_options = []
        for src, dest in add_data.items():
            add_data_options.append("--add-data")
            add_data_options.append(f"{src}{os.path.pathsep}{dest}")

        # Create --hidden-import options from previously created hidden imports list.
        hidden_import_options = []
        for h in hidden_imports:
            hidden_import_options.append("--hidden-import")
            hidden_import_options.append(str(h))

        dist_path = pyinstaller_output / "dist"

        # Run the PyInstaller.
        common.run_command(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                str(scalyr_agent_package_path / "agent_main.py"),
                "--onefile",
                "--distpath",
                str(dist_path),
                "--workpath",
                str(pyinstaller_output / "build"),
                "-n",
                type(self).FROZEN_BINARY_FILE_NAME,
                "--paths",
                ":".join(paths_to_include),
                *add_data_options,
                *hidden_import_options,
                "--exclude-module",
                "asyncio",
                "--exclude-module",
                "FixTk",
                "--exclude-module",
                "tcl",
                "--exclude-module",
                "tk",
                "--exclude-module",
                "_tkinter",
                "--exclude-module",
                "tkinter",
                "--exclude-module",
                "Tkinter",
                "--exclude-module",
                "sqlite",
            ],
            cwd=str(__SOURCE_ROOT__),
        )

        frozen_binary_path = dist_path / type(self).FROZEN_BINARY_FILE_NAME
        # Make frozen binary executable.
        frozen_binary_path.chmod(
            frozen_binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )

        # Copy resulting frozen binary to the output.
        output_path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(frozen_binary_path, output_path)

    @property
    def _agent_install_root_path(self) -> pl.Path:
        """
        The path to the root directory with all agent-related files.
        By the default, the install root of the agent is the same as the root of the whole package.
        """
        return self._package_root_path

    def _build_package_files(self):
        """
        This method builds all files of the package.
        """

        # Clear previously used build folder, if exists.
        if self._build_output_path.exists():
            shutil.rmtree(self._build_output_path)

        self._build_output_path.mkdir(parents=True)
        self._intermediate_results_path.mkdir()
        self._package_root_path.mkdir()

        # Build files in the agent's install root.
        self._build_agent_install_root()

    def _build_agent_install_root(self):
        """
        Build the basic structure of the install root.

            This creates a directory and then populates it with the basic structure required by most of the packages.

            It copies the certs, the configuration directories, etc.

            In the end, the structure will look like:
                certs/ca_certs.pem         -- The trusted SSL CA root list.
                bin/scalyr-agent-2         -- Main agent executable.
                bin/scalyr-agent-2-config  -- The configuration tool executable.
                build_info                 -- A file containing the commit id of the latest commit included in this package,
                                              the time it was built, and other information.
                VERSION                    -- File with current version of the agent.
                install_type               -- File with type of the installation.

        """

        if self._agent_install_root_path.exists():
            shutil.rmtree(self._agent_install_root_path)

        self._agent_install_root_path.mkdir(parents=True)

        # Copy the monitors directory.
        monitors_path = self._agent_install_root_path / "monitors"
        shutil.copytree(__SOURCE_ROOT__ / "monitors", monitors_path)
        recursively_delete_files_by_name(
            self._agent_install_root_path / monitors_path, "README.md"
        )

        # Add VERSION file.
        shutil.copy2(
            __SOURCE_ROOT__ / "VERSION", self._agent_install_root_path / "VERSION"
        )

        # Create bin directory with executables.
        bin_path = self._agent_install_root_path / "bin"
        bin_path.mkdir()

        if type(self).USE_FROZEN_BINARIES:
            self._build_frozen_binary(bin_path)
        else:
            source_code_path = self._agent_install_root_path / "py"

            shutil.copytree(
                __SOURCE_ROOT__ / "scalyr_agent", source_code_path / "scalyr_agent"
            )

            agent_main_executable_path = bin_path / "scalyr-agent-2"
            agent_main_executable_path.symlink_to(
                pl.Path("..", "py", "scalyr_agent", "agent_main.py")
            )

            agent_config_executable_path = bin_path / "scalyr-agent-2-config"
            agent_config_executable_path.symlink_to(
                pl.Path("..", "py", "scalyr_agent", "config_main.py")
            )

            # Write install_info file inside the "scalyr_agent" package.
            build_info_path = source_code_path / "scalyr_agent" / "install_info.json"
            build_info_path.write_text(self._install_info_str)

            # Don't include the tests directories.  Also, don't include the .idea directory created by IDE.
            recursively_delete_dirs_by_name(
                source_code_path, r"\.idea", "tests", "__pycache__"
            )
            recursively_delete_files_by_name(
                source_code_path,
                r".*\.pyc",
                r".*\.pyo",
                r".*\.pyd",
                r"all_tests\.py",
                r".*~",
            )

    @abc.abstractmethod
    def _build(self):
        """
        The implementation of the package build.
        """
        pass


class LinuxPackageBuilder(PackageBuilder):
    """
    The base package builder for all Linux packages.
    """

    EXCLUDED_MONITORS = [
        "windows_event_log_monitor",
        "windows_system_metrics",
        "windows_process_metrics",
    ]

    def _build_agent_install_root(self):
        """
        Add files to the agent's install root which are common for all linux packages.
        """
        super(LinuxPackageBuilder, self)._build_agent_install_root()

        # Add certificates.
        certs_path = self._agent_install_root_path / "certs"
        self._add_certs(certs_path)

        # Misc extra files needed for some features.
        # This docker file is needed by the `scalyr-agent-2-config --docker-create-custom-dockerfile` command.
        # We put it in all distributions (not just the docker_tarball) in case a customer creates an image
        # using a package.
        misc_path = self._agent_install_root_path / "misc"
        misc_path.mkdir()
        for f in ["Dockerfile.custom_agent_config", "Dockerfile.custom_k8s_config"]:
            shutil.copy2(__SOURCE_ROOT__ / "docker" / f, misc_path / f)


class LinuxFhsBasedPackageBuilder(LinuxPackageBuilder):
    """
    The package builder for the packages which follow the Linux FHS structure.
    (https://en.wikipedia.org/wiki/Filesystem_Hierarchy_Standard)
    For example deb, rpm, docker and k8s images.
    """

    INSTALL_TYPE = "package"

    @property
    def _agent_install_root_path(self) -> pl.Path:
        # The install root for FHS based packages is located in the usr/share/scalyr-agent-2.
        original_install_root = super(
            LinuxFhsBasedPackageBuilder, self
        )._agent_install_root_path
        return original_install_root / "usr/share/scalyr-agent-2"

    def _build_package_files(self):

        super(LinuxFhsBasedPackageBuilder, self)._build_package_files()

        pl.Path(self._package_root_path, "var/log/scalyr-agent-2").mkdir(parents=True)
        pl.Path(self._package_root_path, "var/lib/scalyr-agent-2").mkdir(parents=True)

        bin_path = self._agent_install_root_path / "bin"
        usr_sbin_path = self._package_root_path / "usr/sbin"
        usr_sbin_path.mkdir(parents=True)
        for binary_path in bin_path.iterdir():
            binary_symlink_path = (
                self._package_root_path / "usr/sbin" / binary_path.name
            )
            symlink_target_path = pl.Path(
                "..", "share", "scalyr-agent-2", "bin", binary_path.name
            )
            binary_symlink_path.symlink_to(symlink_target_path)


class AgentPackageBuilder(CacheableBuilder):
    def _initialize(self):
        self._package_root_path = self.output_path / "package_root"


class ContainerPackageBuilder(AgentPackageBuilder):
    """
    The base builder for all docker and kubernetes based images . It builds an executable script in the current working
     directory that will build the container image for the various Docker and Kubernetes targets.
     This script embeds all assets it needs in it so it can be a standalone artifact. The script is based on
     `docker/scripts/container_builder_base.sh`. See that script for information on it can be used."
    """
    # Path to the configuration which should be used in this build.
    CONFIG_PATH = None

    # Names of the result image that goes to dockerhub.
    RESULT_IMAGE_NAMES: List[str]

    BASE_IMAGE_BUILDER_STEP: deployments.ShellScriptDeploymentStep

    INPUT = [
        BuilderInput(
            name="--only-filesystem-tarball",
            dest="only_filesystem_tarball",
            required=False,
            help="Build only the tarball with the filesystem of the agent. This argument has to accept"
                 "path to the directory where the tarball is meant to be built. "
                 "Used by the Dockerfile itself and does not required for the manual build.",
        ),
        BuilderInput(
            name="--registry",
            dest="registry",
            help="Registry (or repository) name where to push the result image.",
        ),
        BuilderInput(
            name="--user",
            dest="user",
            help="User name prefix for the image name."
        ),
        BuilderInput(
            name="--tag",
            dest="tag",
            action="append",
            help="The tag that will be applied to every registry that is specified. Can be used multiple times.",
        ),
        BuilderInput(
            name="--push",
            dest="push",
            action="store_true",
            help="Push the result docker image."
        ),
        BuilderInput(
            name="--use-test-version",
            dest="use_test_version",
            action="store_true",
            default=False,
            help="Build a special version of image with additional measuring tools (such as coverage). "
                 "Used only for testing.",
        ),
        BuilderInput(
            name="--platforms",
            dest="platforms",
            help="Comma delimited list of platforms to build (and optionally push) the image for.",
        )
    ]

    # def __init__(
    #     self,
    #     # image_names=None,
    #     # registry: str = None,
    #     # user: str = None,
    #     # tags: List[str] = None,
    #     # push: bool = False,
    #     # use_test_version: bool = False,
    #     # platforms: List[str] = None,
    # ):
    #     """
    #     :param config_path: Path to the configuration directory which will be copied to the image.
    #     :param variant: Adds the specified string into the package's iteration name. This may be None if no additional
    #     tweak to the name is required. This is used to produce different packages even for the same package type (such
    #     as 'rpm').
    #     :param no_versioned_file_name:  True if the version number should not be embedded in the artifact's file name.
    #     """
    #     # self.use_test_version = use_test_version
    #     # self.image_names = image_names
    #     # self.registry = registry
    #     # self.user = user
    #     # self.tags = tags
    #     # self.push = push
    #
    #     super(ContainerPackageBuilder, self).__init__(
    #         required_deployment_steps=[self.base_image_deployment_step]
    #     )

    def _initialize(self):
        self._name = type(self).NAME
        self.config_path = type(self).CONFIG_PATH

        self.dockerfile_path = __SOURCE_ROOT__ / "agent_build/docker/Dockerfile"

        base_image_deployment_step = type(self).BASE_IMAGE_BUILDER_STEP

        self.use_test_version = self._input_values["use_test_version"]
        self.registry = self._input_values["registry"]
        self.tags = self._input_values["tag"]
        self.user = self._input_values["user"]
        self.push = self._input_values["push"]

        self.only_filesystem_tarball = self._input_values["only_filesystem_tarball"]
        if self.only_filesystem_tarball:
            self.only_filesystem_tarball = pl.Path(self._input_values["only_filesystem_tarball"])

        platforms = self._input_values["platforms"]
        if platforms is None:
            self.base_image_deployment_step = base_image_deployment_step
            self.platforms = self.base_image_deployment_step.platforms
        else:
            self.platforms = platforms
            self.base_image_deployment_step = type(base_image_deployment_step)(
                name="agent-docker-base-image-custom",
                platforms=platforms
            )

        super(ContainerPackageBuilder, self)._initialize()

    @property
    def name(self) -> str:
        # Container builders are special since we have an instance per Dockerfile since different
        # Dockerfile represents a different builder
        return self._name

    def _build_package_files(self):

        build_linux_lfs_agent_files(
            copy_agent_source=True,
            output_path=self._package_root_path,
            config_path=type(self).CONFIG_PATH
        )

        # Need to create some docker specific directories.
        pl.Path(self._package_root_path / "var/log/scalyr-agent-2/containers").mkdir()

    def build_filesystem_tarball(self):
        self._build_package_files()

        container_tarball_path = self.output_path / "scalyr-agent.tar.gz"

        # Do a manual walk over the contents of root so that we can use `addfile` to add the tarfile... which allows
        # us to reset the owner/group to root.  This might not be that portable to Windows, but for now, Docker is
        # mainly Posix.
        with tarfile.open(container_tarball_path, "w:gz") as container_tar:

            for root, dirs, files in os.walk(self._package_root_path):
                to_copy = []
                for name in dirs:
                    to_copy.append(os.path.join(root, name))
                for name in files:
                    to_copy.append(os.path.join(root, name))

                for x in to_copy:
                    file_entry = container_tar.gettarinfo(
                        x, arcname=str(pl.Path(x).relative_to(self._package_root_path))
                    )
                    file_entry.uname = "root"
                    file_entry.gname = "root"
                    file_entry.uid = 0
                    file_entry.gid = 0

                    if file_entry.isreg():
                        with open(x, "rb") as fp:
                            container_tar.addfile(file_entry, fp)
                    else:
                        container_tar.addfile(file_entry)

        self.only_filesystem_tarball.parent.mkdir(exist_ok=True, parents=True)
        shutil.copy2(container_tarball_path, self.only_filesystem_tarball)

    def _build(self, locally: bool = False):
        """
        This function builds Agent docker image by using the specified dockerfile (defaults to "Dockerfile").
        It passes to dockerfile its own package type through docker build arguments, so the same package builder
        will be executed inside the docker build to prepare inner container filesystem.

        The result image is built upon the base image that has to be built by the deployment of this builder.
            Since it's not a trivial task to "transfer" a multi-platform image from one place to another, the image is
            pushed to a local registry in the container, and the root of that registry is transferred instead.

        Registry's root in /var/lib/registry is exposed to the host by using docker's mount feature and saved in the
            deployment's output directory. This builder then spins up another local registry container and mounts root
            of the saved registry. Now builder can refer to this local registry in order to get the base image.

        :param image_names: The list of image names. By default uses image names that are specified in the
            package builder.
        :param registry: The registry to push to.
        :param user: User prefix for the image name.
        :param tags: List of tags.
        :param push: If True, push result image to the registries that are specified in the 'registries' argument.
            If False, then just export the result image to the local docker. NOTE: The local docker cannot handle
            multi-platform images, so it will only get image for its  platform.
        :param use_test_version: Makes testing docker image that runs agent with enabled coverage measuring (Python
            'coverage' library). Must not be enabled in production images.
        :param platforms: List of platform names to build the image for.
        """

        if self.only_filesystem_tarball:
            self.build_filesystem_tarball()
            return

        registry_data_path = self.base_image_deployment_step.output_directory / "output_registry"

        # if not common.IN_CICD:
        #     # If there's not a CI/CD then the deployment has to be done explicitly.
        #     # If there is an CI/CD, then the deployment has to be already done.
        #
        #     # The ready deployment is required because it builds the base image of our result image.
        #
        #     # Prepare the deployment output directory and also remove previous one, if exists.
        #     if registry_data_path.is_dir():
        #         try:
        #             shutil.rmtree(registry_data_path)
        #         except PermissionError as e:
        #             if e.errno == 13:
        #                 # NOTE: Depends on the uid user for the registry inside the container, /var/lib/registry
        #                 # inside the container and as such host directory will be owned by root. This is not
        #                 # great, but that's just a quick workaround.
        #                 # Better solution would be for the registry image to suppport setting uid and gid for
        #                 # that directory to the current user or applying 777 permissions + umask to that
        #                 # directory.
        #                 # Just a safe guard to ensure that data path is local to this directory so
        #                 # we don't accidentally delete system stuff.
        #                 cwd = os.getcwd()
        #                 assert str(registry_data_path).startswith(
        #                     str(self.output_path)
        #                 )
        #                 assert str(registry_data_path).startswith(cwd)
        #                 common.check_output_with_log(
        #                     ["sudo", "rm", "-rf", str(registry_data_path)]
        #                 )
        #             else:
        #                 raise e
        #
        #     self._run_deployment_steps()

        # Create docker buildx builder instance. # Without it the result image won't be pushed correctly
        # to the local registry.
        buildx_builder_name = "agent_image_buildx_builder"

        # print docker and buildx version
        docker_version_output = (
            common.check_output_with_log(["docker", "version"]).decode().strip()
        )
        logging.info(f"Using docker version:\n{docker_version_output}\n")

        buildx_version_output = (
            common.check_output_with_log(["docker", "buildx", "version"])
            .decode()
            .strip()
        )
        logging.info(f"Using buildx version {buildx_version_output}")

        # check if builder already exists.
        ls_output = (
            common.check_output_with_log(["docker", "buildx", "ls"]).decode().strip()
        )

        if buildx_builder_name not in ls_output:
            # Build new buildx builder
            logging.info(f"Create new buildx builder instance '{buildx_builder_name}'.")
            common.run_command(
                [
                    "docker",
                    "buildx",
                    "create",
                    # This option is important, without it the image won't be pushed to the local registry.
                    "--driver-opt=network=host",
                    "--name",
                    buildx_builder_name,
                ]
            )

        # Use builder.
        logging.info(f"Use buildx builder instance '{buildx_builder_name}'.")
        common.run_command(
            [
                "docker",
                "buildx",
                "use",
                buildx_builder_name,
            ]
        )

        logging.info("Build base image.")

        base_image_tag_suffix = (
            self.base_image_deployment_step.python_image_suffix
        )

        base_image_name = f"agent_base_image:{base_image_tag_suffix}"

        if self.use_test_version:
            logging.info("Build testing image version.")
            base_image_name = f"{base_image_name}-testing"

        registry = self.registry or ""
        tags = self.tags or ["latest"]

        if not os.path.isfile(self.dockerfile_path):
            raise ValueError(f"File path {self.dockerfile_path} doesn't exist")

        tag_options = []

        image_names = type(self).RESULT_IMAGE_NAMES[:]
        for image_name in image_names:

            full_name = image_name

            if self.user:
                full_name = f"{self.user}/{full_name}"

            if registry:
                full_name = f"{registry}/{full_name}"

            for tag in tags:
                tag_options.append("-t")

                full_name_with_tag = f"{full_name}:{tag}"

                tag_options.append(full_name_with_tag)

        command_options = [
            "docker",
            "buildx",
            "build",
            *tag_options,
            "-f",
            str(self.dockerfile_path),
            "--build-arg",
            f"BUILD_TYPE={type(self).PACKAGE_TYPE.value}",
            "--build-arg",
            f"BUILDER_NAME={self.name}",
            "--build-arg",
            f"BASE_IMAGE=localhost:5005/{base_image_name}",
            "--build-arg",
            f"BASE_IMAGE_SUFFIX={base_image_tag_suffix}",
        ]

        if common.DEBUG:
            # If debug, then also specify the debug mode inside the docker build.
            command_options.extend(
                [
                    "--build-arg",
                    "AGENT_BUILD_DEBUG=1",
                ]
            )

        # If we need to push, then specify all platforms.
        if self.push:
            for plat in self.platforms:
                command_options.append("--platform")
                command_options.append(plat)

        if self.use_test_version:
            # Pass special build argument to produce testing image.
            command_options.append("--build-arg")
            command_options.append("MODE=testing")

        if self.push:
            command_options.append("--push")
        else:
            command_options.append("--load")

        command_options.append(str(__SOURCE_ROOT__))

        build_log_message = f"Build images:  {image_names}"
        if self.push:
            build_log_message = f"{build_log_message} and push."
        else:
            build_log_message = (
                f"{build_log_message} and load result image to local docker."
            )

        logging.info(build_log_message)

        # Create container with local image registry. And mount existing registry root with base images.
        registry_container = build_in_docker.LocalRegistryContainer(
            name="agent_image_output_registry",
            registry_port=5005,
            registry_data_path=registry_data_path,
        )

        # Start registry and run build of the final docker image. Build process will refer the the
        # base image in the local registry.
        with registry_container:
            common.run_command(
                command_options,
                # This command runs partially runs the same code, so it would be nice to see the output.
                debug=True,
            )

    # @classmethod
    # def handle_command_line_arguments2(cls, argv=None):
    #     parser = argparse.ArgumentParser()
    #
    #     # Define argument for all packages
    #     parser.add_argument(
    #         "--locally",
    #         action="store_true",
    #         help="Perform the build on the current system which runs the script. Without that, some packages may be built "
    #              "by default inside the docker.",
    #     )
    #
    #     parser.add_argument(
    #         "--no-versioned-file-name",
    #         action="store_true",
    #         dest="no_versioned_file_name",
    #         default=False,
    #         help="If true, will not embed the version number in the artifact's file name.  This only "
    #              "applies to the `tarball` and container builders artifacts.",
    #     )
    #
    #     parser.add_argument(
    #         "-v",
    #         "--variant",
    #         dest="variant",
    #         default=None,
    #         help="An optional string that is included in the package name to identify a variant "
    #              "of the main release created by a different packager.  "
    #              "Most users do not need to use this option.",
    #     )
    #
    #     parser.add_argument(
    #         "--debug",
    #         action="store_true",
    #         help="Enable debug mode with additional logging.",
    #     )
    #
    #     parser.add_argument(
    #         "--only-filesystem-tarball",
    #         dest="only_filesystem_tarball",
    #         help="Build only the tarball with the filesystem of the agent. This argument has to accept"
    #              "path to the directory where the tarball is meant to be built. "
    #              "Used by the Dockerfile itself and does not required for the manual build.",
    #     )
    #
    #     parser.add_argument(
    #         "--registry",
    #         help="Registry (or repository) name where to push the result image.",
    #     )
    #
    #     parser.add_argument(
    #         "--user", help="User name prefix for the image name."
    #     )
    #
    #     parser.add_argument(
    #         "--tag",
    #         action="append",
    #         help="The tag that will be applied to every registry that is specified. Can be used multiple times.",
    #     )
    #
    #     parser.add_argument(
    #         "--push", action="store_true", help="Push the result docker image."
    #     )
    #
    #     parser.add_argument(
    #         "--coverage",
    #         dest="coverage",
    #         action="store_true",
    #         default=False,
    #         help="Enable coverage analysis. Can be used in smoketests. Only works with docker/k8s.",
    #     )
    #
    #     parser.add_argument(
    #         "--platforms",
    #         dest="platforms",
    #         help="Comma delimited list of platforms to build (and optionally push) the image for.",
    #     )
    #
    #     if argv is None:
    #         argv = sys.argv[:]
    #
    #     args = parser.parse_args(args=argv)
    #
    #     if args.platforms:
    #         platforms = args.platforms.split(",")
    #     else:
    #         platforms = None
    #
    #     package_builder = cls(
    #         push=args.push,
    #         registry=args.registry,
    #         user=args.user,
    #         tags=args.tag or [],
    #         use_test_version=args.coverage,
    #         platforms=platforms
    #     )
    #     if args.only_filesystem_tarball:
    #         # Build only image filesystem.
    #         package_builder.build_filesystem_tarball(
    #             output_path=pl.Path(args.only_filesystem_tarball)
    #         )
    #         exit(0)
    #
    #     package_builder.build()
    #     exit(0)

    # @classmethod
    # def add_command_line_arguments33(cls, parser):
    #
    #     # Define argument for all packages
    #     parser.add_argument(
    #         "--locally",
    #         action="store_true",
    #         help="Perform the build on the current system which runs the script. Without that, some packages may be built "
    #              "by default inside the docker.",
    #     )
    #
    #     parser.add_argument(
    #         "--no-versioned-file-name",
    #         action="store_true",
    #         dest="no_versioned_file_name",
    #         default=False,
    #         help="If true, will not embed the version number in the artifact's file name.  This only "
    #              "applies to the `tarball` and container builders artifacts.",
    #     )
    #
    #     parser.add_argument(
    #         "-v",
    #         "--variant",
    #         dest="variant",
    #         default=None,
    #         help="An optional string that is included in the package name to identify a variant "
    #              "of the main release created by a different packager.  "
    #              "Most users do not need to use this option.",
    #     )
    #
    #     parser.add_argument(
    #         "--debug",
    #         action="store_true",
    #         help="Enable debug mode with additional logging.",
    #     )
    #
    #     parser.add_argument(
    #         "--only-filesystem-tarball",
    #         dest="only_filesystem_tarball",
    #         help="Build only the tarball with the filesystem of the agent. This argument has to accept"
    #              "path to the directory where the tarball is meant to be built. "
    #              "Used by the Dockerfile itself and does not required for the manual build.",
    #     )
    #
    #     parser.add_argument(
    #         "--registry",
    #         help="Registry (or repository) name where to push the result image.",
    #     )
    #
    #     parser.add_argument(
    #         "--user", help="User name prefix for the image name."
    #     )
    #
    #     parser.add_argument(
    #         "--tag",
    #         action="append",
    #         help="The tag that will be applied to every registry that is specified. Can be used multiple times.",
    #     )
    #
    #     parser.add_argument(
    #         "--push", action="store_true", help="Push the result docker image."
    #     )
    #
    #     parser.add_argument(
    #         "--coverage",
    #         dest="coverage",
    #         action="store_true",
    #         default=False,
    #         help="Enable coverage analysis. Can be used in smoketests. Only works with docker/k8s.",
    #     )
    #
    #     parser.add_argument(
    #         "--platforms",
    #         dest="platforms",
    #         help="Comma delimited list of platforms to build (and optionally push) the image for.",
    #     )
    # @classmethod
    # def handle_command_line_arguments44(cls, args):
    #
    #     if args.platforms:
    #         platforms = args.platforms.split(",")
    #     else:
    #         platforms = None
    #
    #     package_builder = cls(
    #         push=args.push,
    #         registry=args.registry,
    #         user=args.user,
    #         tags=args.tag or [],
    #         use_test_version=args.coverage,
    #         platforms=platforms
    #     )
    #     if args.only_filesystem_tarball:
    #         # Build only image filesystem.
    #         package_builder.build_filesystem_tarball(
    #             output_path=pl.Path(args.only_filesystem_tarball)
    #         )
    #         exit(0)
    #
    #     package_builder.build()
    #     exit(0)



# class K8sPackageBuilder(ContainerPackageBuilder):
#     """
#     An image for running the agent on Kubernetes.
#     """
#
#     PACKAGE_TYPE = constants.PackageType.K8S
#     RESULT_IMAGE_NAMES = ["scalyr-k8s-agent"]
#
#
# class DockerJsonPackageBuilder(ContainerPackageBuilder):
#     """
#     An image for running on Docker configured to fetch logs via the file system (the container log
#     directory is mounted to the agent container.)  This is the preferred way of running on Docker.
#     This image is published to scalyr/scalyr-agent-docker-json.
#     """
#
#     PACKAGE_TYPE = constants.PackageType.DOCKER_JSON
#     RESULT_IMAGE_NAMES = ["scalyr-agent-docker-json"]
#
#
# class DockerSyslogPackageBuilder(ContainerPackageBuilder):
#     """
#     An image for running on Docker configured to receive logs from other containers via syslog.
#     This is the deprecated approach (but is still published under scalyr/scalyr-docker-agent for
#     backward compatibility.)  We also publish this under scalyr/scalyr-docker-agent-syslog to help
#     with the eventual migration.
#     """
#
#     PACKAGE_TYPE = constants.PackageType.DOCKER_SYSLOG
#     RESULT_IMAGE_NAMES = [
#         "scalyr-agent-docker-syslog",
#         "scalyr-agent-docker",
#     ]
#
#
# class DockerApiPackageBuilder(ContainerPackageBuilder):
#     """
#     An image for running on Docker configured to fetch logs via the Docker API using docker_raw_logs: false
#     configuration option.
#     """
#
#     PACKAGE_TYPE = constants.PackageType.DOCKER_API
#     RESULT_IMAGE_NAMES = ["scalyr-agent-docker-api"]


_CONFIGS_PATH = __SOURCE_ROOT__ / "docker"

# DEBIAN_DOCKER_BASE_IMAGE_BUILD_STEP = deployments.BuildDebianDockerBaseImageStep(
#     platforms=AGENT_DOCKER_IMAGE_SUPPORTED_PLATFORMS
# )
#
# ALPINE_DOCKER_BASE_IMAGE_BUILD_STEP = deployments.BuildAlpineDockerBaseImageStep(
#     platforms=AGENT_DOCKER_IMAGE_SUPPORTED_PLATFORMS
# )

ALL_DEPLOYMENT_STEPS_LIST = []
#
# ALL_DEPLOYMENT_STEPS_LIST.append(DEBIAN_DOCKER_BASE_IMAGE_BUILD_STEP)
# ALL_DEPLOYMENT_STEPS_LIST.append(ALPINE_DOCKER_BASE_IMAGE_BUILD_STEP)


_DISTRO_TO_PYTHON_IMAGE_SUFFIX = {
    "debian": "slim",
    "alpine": "alpine"
}

# CPU architectures or platforms that has to be supported by the Agent docker images,
_AGENT_DOCKER_IMAGE_SUPPORTED_PLATFORMS = [
    Architecture.X86_64.as_docker_platform.value,
    Architecture.ARM64.as_docker_platform.value,
    Architecture.ARMV7.as_docker_platform.value,
]

_AGENT_DOCKER_IMAGES_RESULT_IMAGE_NAME = {
    "docker-json": ["scalyr-agent-docker-json"],
    "docker-syslog": [
        "scalyr-agent-docker-syslog",
        "scalyr-agent-docker",
    ],
    "docker-api": ["scalyr-agent-docker-api"],
    "k8s": ["scalyr-k8s-agent"]
}

DOCKER_IMAGE_PACKAGE_BUILDERS = {}
_AGENT_BUILD_DOCKER_PATH = _AGENT_BUILD_PATH / "docker"
_AGENT_BUILD_DEPLOYMENT_STEPS_PATH = _AGENT_BUILD_PATH / "tools/environment_deployments/steps"
_AGENT_BUILD_REQUIREMENTS_FILES_PATH = _AGENT_BUILD_PATH / "requirement-files"
for distro_name in ["debian", "alpine"]:

    base_docker_image_step = deployments.BuildDockerBaseImageStep(
        name=f"agent-docker-base-image-{distro_name}",
        python_image_suffix=_DISTRO_TO_PYTHON_IMAGE_SUFFIX[distro_name],
        platforms=_AGENT_DOCKER_IMAGE_SUPPORTED_PLATFORMS,
    )

    #ALL_DEPLOYMENT_STEPS_LIST.append(base_docker_image_step)

    for agent_package_type in [
        PackageType.DOCKER_JSON,
        PackageType.DOCKER_SYSLOG,
        PackageType.DOCKER_API,
        PackageType.K8S
    ]:
        class DockerImageBuilder(ContainerPackageBuilder):
            NAME = f"{agent_package_type.value}-{distro_name}"
            PACKAGE_TYPE = agent_package_type
            CONFIG_PATH = _CONFIGS_PATH / f"{agent_package_type.value}-config"
            RESULT_IMAGE_NAMES = _AGENT_DOCKER_IMAGES_RESULT_IMAGE_NAME[agent_package_type.value]
            BASE_IMAGE_BUILDER_STEP = DEPLOYMENT_STEP = base_docker_image_step

        DOCKER_IMAGE_PACKAGE_BUILDERS[DockerImageBuilder.NAME] = DockerImageBuilder


BUILD_PYTHON_BASE = ShellScriptDeploymentStep(
    name="build_python_base",
    script_path=pl.Path("agent_build/tools/environment_deployments/steps/frozen_binaries_prepare_base.sh"),
    architecture=Architecture.X86_64,
    previous_step="centos:6",
    cacheable=True
)


# class BuildPython(ShellScriptDeploymentStep):
#     CACHEABLE_STEPS = [BUILD_PYTHON_BASE]
#     def __init__(
#         self,
#         name: str,
#         architecture: constants.Architecture,
#     ):
#
#         super(BuildPython, self).__init__(
#             name=name,
#             architecture=architecture,
#             previous_step=BUILD_PYTHON_BASE,
#             cacheable=True
#         )
#
#     @property
#     def script_path(self) -> pl.Path:
#         return pl.Path("agent_build/tools/environment_deployments/steps/build_python.sh")


BUILD_PYTHON_STEP = ShellScriptDeploymentStep(
    name="build_python",
    architecture=Architecture.X86_64,
    script_path=pl.Path("agent_build/tools/environment_deployments/steps/frozen_binaries_build_python.sh"),
    previous_step=BUILD_PYTHON_BASE,
    cacheable=True
)


# class InstallAgentDependenciesStep(ShellScriptDeploymentStep):
#     CACHEABLE_STEPS = [BUILD_PYTHON_STEP]
#
#     def __init__(
#             self,
#             name: str,
#             architecture: constants.Architecture,
#     ):
#         super(InstallAgentDependenciesStep, self).__init__(
#             name=name,
#             architecture=architecture,
#             previous_step=BUILD_PYTHON_STEP,
#             cacheable=True
#         )
#
#     @property
#     def script_path(self) -> pl.Path:
#         return pl.Path("agent_build/tools/environment_deployments/steps/install_agent_python-dependencies.sh")
#
#     @property
#     def _tracked_file_globs(self) -> List[pl.Path]:
#         globs = super(InstallAgentDependenciesStep, self)._tracked_file_globs
#         globs.extend([
#             pl.Path("agent_build/requirement-files/main-requirements.txt"),
#             pl.Path("agent_build/requirement-files/compression-requirements.txt"),
#             pl.Path("agent_build/requirement-files/frozen-binaries-requirements.txt")
#         ])
#         return globs


INSTALL_AGENT_DEPENDENCIES_STEP = ShellScriptDeploymentStep(
    name="install_agent_dependencies",
    architecture=Architecture.X86_64,
    script_path=pl.Path(
        "agent_build/tools/environment_deployments/steps/frozen_binaries_install_agent_python-dependencies.sh"
    ),
    tracked_file_globs=[
        pl.Path("agent_build/requirement-files/main-requirements.txt"),
        pl.Path("agent_build/requirement-files/compression-requirements.txt"),
        pl.Path("agent_build/requirement-files/frozen-binaries-requirements.txt")
    ],
    previous_step=BUILD_PYTHON_STEP,
    cacheable=True,
)


class BuildFrozenBinaryPython(CacheableBuilder):
    NAME = "frozen_binary_builder"
    DEPLOYMENT_STEP = INSTALL_AGENT_DEPENDENCIES_STEP

    INPUT = [
        BuilderInput(
            name="--install_type",
            dest="install_type"
        )
    ]

    def _initialize(self):
        self.install_type = self._input_values["install_type"]

    def _build(self):
        scalyr_agent_package_path = SOURCE_ROOT / "scalyr_agent"

        # Add monitor modules as hidden imports, since they are not directly imported in the agent's code.
        all_builtin_monitor_module_names = [
            mod_path.stem
            for mod_path in pl.Path(
                SOURCE_ROOT, "scalyr_agent", "builtin_monitors"
            ).glob("*.py")
            if mod_path.stem != "__init__"
        ]

        # Define builtin monitors that have to be excluded from particular platform.
        if platform.system().lower().startswith("linux"):
            monitors_to_exclude = [
                "scalyr_agent.builtin_monitors.windows_event_log_monitor",
                "scalyr_agent.builtin_monitors.windows_system_metrics",
                "scalyr_agent.builtin_monitors.windows_process_metrics",
            ]
        elif platform.system().lower().startswith("win"):
            monitors_to_exclude = [
                "scalyr_agent.builtin_monitors.linux_process_metrics.py",
                "scalyr_agent.builtin_monitors.linux_system_metrics.py"
            ]
        else:
            monitors_to_exclude = []

        monitors_to_import = set(all_builtin_monitor_module_names) - set(monitors_to_exclude)

        # # Add packages to frozen binary paths.
        # paths_to_include = [
        #     str(scalyr_agent_package_path),
        #     str(scalyr_agent_package_path / "builtin_monitors"),
        # ]
        #
        # # Add platform specific things.
        # if platform.system().lower().startswith("linux"):
        #     tcollectors_path = pl.Path(
        #         source_root,
        #         "scalyr_agent",
        #         "third_party",
        #         "tcollector",
        #         "collectors",
        #     )

        agent_package_path = os.path.join(SOURCE_ROOT, "scalyr_agent")

        instalL_info_path = self.output_path / "install_info.json"

        install_info = get_install_info(
            install_type=self.install_type
        )

        logging.critical(f"EEEE {install_info}")

        instalL_info_path.write_text(json.dumps(install_info))
        add_data = {instalL_info_path: "scalyr_agent"}

        hidden_imports = [*monitors_to_import, "win32timezone"]

        # Create --add-data options from previously added files.
        add_data_options = []
        for src, dest in add_data.items():
            add_data_options.append("--add-data")
            add_data_options.append("{}{}{}".format(src, os.path.pathsep, dest))

        # Create --hidden-import options from previously created hidden imports list.
        hidden_import_options = []
        for h in hidden_imports:
            hidden_import_options.append("--hidden-import")
            hidden_import_options.append(str(h))

        # paths_options = []
        # for p in paths_to_include:
        #     paths_options.extend(["--paths", p])

        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "-n",
            "scalyr-agent-2",
            os.path.join(agent_package_path, "agent_main.py"),
        ]
        command.extend(add_data_options)
        command.extend(hidden_import_options)
        # command.extend(paths_options)
        command.extend(
            [
                "--exclude-module",
                "asyncio",
                "--exclude-module",
                "FixTk",
                "--exclude-module",
                "tcl",
                "--exclude-module",
                "tk",
                "--exclude-module",
                "_tkinter",
                "--exclude-module",
                "tkinter",
                "--exclude-module",
                "Tkinter",
                "--exclude-module",
                "sqlite",
            ]
        )

        pyinstaller_output = self.output_path / "pyinstaller"
        pyinstaller_output.mkdir(parents=True)

        subprocess.check_call(
            command,
            cwd=str(pyinstaller_output)
        )

        if platform.system().lower().startswith("win"):
            frozen_binary_file_name = "scalyr-agent-2.exe"
        else:
            frozen_binary_file_name = "scalyr-agent-2"

        frozen_binary_path = pyinstaller_output / "dist" / frozen_binary_file_name
        # Make frozen binary executable.
        frozen_binary_path.chmod(
            frozen_binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )

        shutil.copy2(
            frozen_binary_path,
            self.output_path / frozen_binary_file_name
        )

        subprocess.check_call(
            f"ls {self.output_path}", shell=True
        )


# class PrepareFpmBuilder(ShellScriptDeploymentStep):
#     def __init__(
#         self,
#         name: str,
#         architecture: constants.Architecture,
#     ):
#
#         super(PrepareFpmBuilder, self).__init__(
#             name=name,
#             architecture=architecture,
#             previous_step="ubuntu:20.04",
#             cacheable=True
#         )
#
#     @property
#     def script_path(self) -> pl.Path:
#         return pl.Path("agent_build/tools/environment_deployments/steps/prepare_fpm_builder.sh")


PREPARE_FPM_BUILDER = ShellScriptDeploymentStep(
    name="fpm_builder",
    architecture=Architecture.X86_64,
    script_path=pl.Path("agent_build/tools/environment_deployments/steps/prepare_fpm_builder.sh"),
    previous_step="ubuntu:20.04",
    cacheable=True,
)


class FpmBasedPackageBuilder(AgentPackageBuilder):
    ARCHITECTURE: Architecture
    PACKAGE_TYPE: PackageType
    REQUIRED_BUILDER_CLASSES = [BuildFrozenBinaryPython]
    DEPLOYMENT_STEP = PREPARE_FPM_BUILDER

    _FPM_PACKAGE_TYPES = {
        PackageType.DEB: "deb",
        PackageType.RPM: "rpm"
    }
    _FPM_ARCHITECTURES = {
        PackageType.DEB: {
            Architecture.X86_64: "amd64",
            Architecture.ARM64: "arm64"
        },
        PackageType.RPM: {
            Architecture.X86_64: "amd64",
            Architecture.ARM64: "arm64"
        }
    }

    INPUT = [
        BuilderInput(
            name="--variant",
            dest="variant"
        )
    ]

    def _initialize(self):
        self.build_frozen_binary = BuildFrozenBinaryPython.create(
            install_type="package",
        )

        self.variant = self._input_values["variant"]
        self.required_builders = [self.build_frozen_binary]
        super(FpmBasedPackageBuilder, self)._initialize()

    def _build(self):

        build_linux_lfs_agent_files(
            copy_agent_source=False,
            output_path=self._package_root_path,
            config_path=SOURCE_ROOT / "config"
        )

        if self.variant is not None:
            iteration_arg = "--iteration 1.%s" % self.variant
        else:
            iteration_arg = ""

        install_scripts_path = SOURCE_ROOT / "installer/scripts"

        # generate changelogs
        changelogs_path = self.output_path / "package_changelogs"
        create_change_logs(
            output_directory=changelogs_path
        )

        description = (
            "Scalyr Agent 2 is the daemon process Scalyr customers run on their servers to collect metrics and "
            "log files and transmit them to Scalyr."
        )

        # filename = f"scalyr-agent-2_{self._package_version}_{arch}.{ext}"

        version_file_path = SOURCE_ROOT / "VERSION"
        package_version = version_file_path.read_text().strip()

        cls = type(self)
        fpm_architectures = cls._FPM_ARCHITECTURES[cls.PACKAGE_TYPE]
        # fmt: off
        fpm_command = [
            "fpm",
            "-s", "dir",
            "-a", fpm_architectures[cls.ARCHITECTURE],
            "-t", cls._FPM_PACKAGE_TYPES[cls.PACKAGE_TYPE],
            "-n", "scalyr-agent-2",
            "-v", package_version,
            "--chdir", str(self._package_root_path),
            "--license", "Apache 2.0",
            "--vendor", f"Scalyr {iteration_arg}",
            "--maintainer", "czerwin@scalyr.com",
            "--provides", "scalyr-agent-2",
            "--description", description,
            "--depends", 'bash >= 3.2',
            "--url", "https://www.scalyr.com",
            "--deb-user", "root",
            "--deb-group", "root",
            "--deb-changelog", str(changelogs_path / 'changelog-deb'),
            "--rpm-changelog", str(changelogs_path / 'changelog-rpm'),
            "--rpm-user", "root",
            "--rpm-group", "root",
            "--after-install", str(install_scripts_path / 'postinstall.sh'),
            "--before-remove", str(install_scripts_path / 'preuninstall.sh'),
            "--deb-no-default-config-files",
            "--no-deb-auto-config-files",
            "--config-files", "/etc/scalyr-agent-2/agent.json",
            "--directories", "/usr/share/scalyr-agent-2",
            "--directories", "/var/lib/scalyr-agent-2",
            "--directories", "/var/log/scalyr-agent-2",
            # NOTE 1: By default fpm won't preserve all the permissions we set on the files so we need
            # to use those flags.
            # If we don't do that, fpm will use 77X for directories and we don't really want 7 for
            # "group" and it also means config file permissions won't be correct.
            # NOTE 2: This is commented out since it breaks builds produced on builder VM where
            # build_package.py runs as rpmbuilder user (uid 1001) and that uid is preserved as file
            # owner for the package tarball file which breaks things.
            # On Circle CI uid of the user under which the package job runs is 0 aka root so it works
            # fine.
            # We don't run fpm as root on builder VM which means we can't use any other workaround.
            # Commenting this flag out means that original file permissions (+ownership) won't be
            # preserved which means we will also rely on postinst step fixing permissions for fresh /
            # new installations since those permissions won't be correct in the package artifact itself.
            # Not great.
            # Once we move all the build steps to Circle CI and ensure build_package.py runs as uid 0
            # we should uncomment this.
            # In theory it should work wth --*-user fpm flag, but it doesn't. Keep in mind that the
            # issue only applies to deb packages since --rpm-user and --rpm-root flag override the user
            # even if the --rpm-use-file-permissions flag is used.
            # "  --rpm-use-file-permissions "
            "--rpm-use-file-permissions",
            "--deb-use-file-permissions",
            # NOTE: Sadly we can't use defattrdir since it breakes permissions for some other
            # directories such as /etc/init.d and we need to handle that in postinst :/
            # "  --rpm-auto-add-directories "
            # "  --rpm-defattrfile 640"
            # "  --rpm-defattrdir 751"
            # "  -C root usr etc var",
        ]
        # fmt: on

        # Run fpm command and build the package.
        subprocess.check_call(
            fpm_command,
            cwd=str(self.output_path),
        )


FPM_BASED_BUILDERS = {}
for arch in [
    Architecture.X86_64
]:
    class DebPackageBuilder(FpmBasedPackageBuilder):
        NAME = f"deb_{arch.value}"
        ARCHITECTURE = arch
        PACKAGE_TYPE = PackageType.DEB
        INSTALL_TYPE = "package"

    class RpmPackageBuilder(FpmBasedPackageBuilder):
        NAME = f"rpm_{arch.value}"
        ARCHITECTURE = arch
        PACKAGE_TYPE = PackageType.RPM
        INSTALL_TYPE = "package"

    FPM_BASED_BUILDERS.update({
        DebPackageBuilder.NAME: DebPackageBuilder,
        RpmPackageBuilder.NAME: RpmPackageBuilder
    })


class BuildTestEnvironment(CacheableStepsRunner):
    NAME = "test_environment"
    CACHEABLE_DEPLOYMENT_STEPS = [deployments.INSTALL_TEST_REQUIREMENT_STEP]


ALL_PACKAGE_BUILDERS = {
    **DOCKER_IMAGE_PACKAGE_BUILDERS,
    **FPM_BASED_BUILDERS
}

ALL_BUILDERS: Dict[str, Type["CacheableBuilder"]] = {
    **ALL_PACKAGE_BUILDERS,
    BuildTestEnvironment.NAME: BuildTestEnvironment,
    BuildFrozenBinaryPython.NAME: BuildFrozenBinaryPython
}

ALL_DEPLOYMENT_STEPS = {
    step.id: step for step in ALL_DEPLOYMENT_STEPS_LIST
}


a=10
