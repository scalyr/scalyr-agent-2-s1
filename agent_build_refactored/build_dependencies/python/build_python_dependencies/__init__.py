from .build_xz import BuildXZStep
from .build_sqlite import BuildPythonSqliteStep
from .build_zlib import BuildPythonZlibStep
from .build_bzip import BuildPythonBzipStep
from .build_util_linux import BuildPythonUtilLinuxStep
from .build_ncurses import BuildPythonNcursesStep
from .build_libedit import BuildPythonLibeditStep
from .build_libffi import BuildPythonLibffiStep
from .build_openssl import BuildPythonOpenSSLStep

import pathlib as pl

from agent_build_refactored.tools.constants import CpuArch, LibC
from agent_build_refactored.tools.builder import BuilderStep
from agent_build_refactored.build_dependencies.python.download_sources import DownloadSourcesStep
from agent_build_refactored.build_dependencies.python.prepare_build_base import PrepareBuildBaseStep


_PARENT_DIR = pl.Path(__file__).parent


class BuildPytonDependenciesStep(BuilderStep):
    def __init__(
        self,
        download_sources_step: DownloadSourcesStep,
        prepare_build_base: PrepareBuildBaseStep,
        install_prefix: pl.Path,
        architecture: CpuArch,
        libc: LibC,
    ):
        self.download_sources_step = download_sources_step
        self.prepare_build_base = prepare_build_base
        self.install_prefix = install_prefix
        super(BuildPytonDependenciesStep, self).__init__(
            name="build_python_dependencies",
            context=_PARENT_DIR,
            dockerfile=_PARENT_DIR / "Dockerfile",
            build_contexts=[
                self.download_sources_step,
                self.prepare_build_base,
            ],
            build_args={
                "INSTALL_PREFIX": str(install_prefix),
                "ARCH": architecture.value,
                "LIBC": libc.value,
            },
        )
