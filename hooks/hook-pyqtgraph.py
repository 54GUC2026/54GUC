"""Local hook for the plotting dependency in frozen builds.

Keep normal package data and multiprocessing imports, but omit example apps.
The remote-view module imports the multiprocessing submodule during package
initialization, so excluding that submodule would break frozen startup.

The build stage removes only the runtime worker hook that could re-enter the
frozen executable as a second GUI process. Normal imports remain available.
"""

from PyInstaller.utils.hooks import collect_data_files


datas = collect_data_files("pyqtgraph")
hiddenimports = []
excludedimports = [
    "pyqtgraph.examples",
    "pyqtgraph.examples.ExampleApp",
]
