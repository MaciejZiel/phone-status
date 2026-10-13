The service itself has no third-party runtime dependencies (Python standard library only, no CDN assets, system font stack). The entries below are development and CI tools from `requirements-dev.txt` and the CI workflow.

| source | name | license type | license link |
| --- | --- | --- | --- |
| https://github.com/pytest-dev/pytest | pytest | MIT | https://github.com/pytest-dev/pytest/blob/main/LICENSE |
| https://github.com/astral-sh/ruff | ruff | MIT | https://github.com/astral-sh/ruff/blob/main/LICENSE |
| https://github.com/pytest-dev/iniconfig | iniconfig | MIT | https://github.com/pytest-dev/iniconfig/blob/main/LICENSE |
| https://github.com/pytest-dev/pluggy | pluggy | MIT | https://github.com/pytest-dev/pluggy/blob/main/LICENSE |
| https://github.com/pypa/packaging | packaging | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging/blob/main/LICENSE |
| https://github.com/pygments/pygments | Pygments | BSD-2-Clause | https://github.com/pygments/pygments/blob/master/LICENSE |
| https://github.com/microsoft/pyright | pyright 1.1.414 (npm, CI type checking only) | MIT | https://github.com/microsoft/pyright/blob/main/LICENSE.txt |
| https://github.com/fsevents/fsevents | fsevents (optional macOS-only dependency of pyright, not installed on Linux CI) | MIT | https://github.com/fsevents/fsevents/blob/master/LICENSE |
