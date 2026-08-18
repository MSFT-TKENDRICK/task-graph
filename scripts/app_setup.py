"""Create `.venv` and install task-graph into it.

This is what the GitHub Copilot app's **Setup** script runs, once, when it
creates a session. It also backs `.\\run.ps1 setup`, so there is one
provisioning path rather than two that drift apart.

It runs *before* the virtualenv exists, from whichever shell the app happens to
use, so it is stdlib only, imports nothing from ``task_graph``, and assumes
neither PowerShell nor ``uv``.

**Index discovery is the part that actually matters.** On the Microsoft
corporate network ``files.pythonhosted.org`` refuses the TLS handshake outright,
so the default PyPI index cannot serve wheels at all and the bare ``uv pip
install`` in older docs dies on the first package. pip already knows the
internal mirror -- it is configured machine-wide in
``C:\\ProgramData\\pip\\pip.ini`` -- so rather than hard-coding a
Microsoft-internal URL into a git repository, this asks pip for its own
effective index and hands that to uv. Off the corporate network pip reports
nothing, uv uses PyPI, and the identical code path is correct.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"

#: ``pip config list`` prints ``<scope>.<key>='<value>'``. Later scopes in this
#: tuple override earlier ones, which is pip's own precedence order.
_INDEX_SCOPES = ("global", "user", "site", "install")
_INDEX_LINE = re.compile(r"^(?P<scope>\w+)\.index-url='(?P<url>[^']+)'\s*$")


def venv_python(root: Path = ROOT) -> Path:
    """Return the interpreter inside ``root/.venv`` for this platform."""
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


def run(command: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run ``command`` from the repo root, echoing it first."""
    print(f"$ {subprocess.list2cmdline(command)}", flush=True)
    return subprocess.call(command, cwd=str(ROOT), env=env)


def discover_index_url(python: Path) -> str | None:
    """Return the package index pip is configured to use, if any.

    An index already named in the environment wins: someone who exported
    ``UV_INDEX_URL`` or ``PIP_INDEX_URL`` has said what they want.
    """
    for name in ("UV_INDEX_URL", "PIP_INDEX_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        output = subprocess.run(
            [str(python), "-m", "pip", "config", "list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    found: dict[str, str] = {}
    for line in output.splitlines():
        match = _INDEX_LINE.match(line.strip())
        if match:
            found[match.group("scope")] = match.group("url")
    for scope in reversed(_INDEX_SCOPES):
        if scope in found:
            return found[scope]
    return None


def ensure_venv() -> Path:
    """Create ``.venv`` if it is missing and return its interpreter.

    Deliberately ``python -m venv`` rather than ``uv venv``: the stdlib builder
    always puts pip inside, and pip is both the fallback installer and the only
    thing that knows this machine's index configuration.
    """
    python = venv_python()
    if python.exists():
        step(f"Reusing {VENV}")
        return python

    step(f"Creating virtualenv at {VENV}")
    if run([sys.executable, "-m", "venv", str(VENV)]) != 0:
        raise SystemExit("could not create .venv")
    if not python.exists():
        raise SystemExit(f"virtualenv created but {python} is missing")
    return python


def install_with_uv(python: Path, index_url: str | None) -> bool:
    """Install via uv when it is present; return False if it did not work.

    ``--native-tls`` makes uv trust the platform certificate store, which is
    what a TLS-inspecting corporate network needs and is harmless elsewhere.
    """
    uv = shutil.which("uv")
    if not uv:
        return False

    env = dict(os.environ)
    if index_url:
        env["UV_INDEX_URL"] = index_url
    step("Installing task-graph and dev dependencies (uv)")
    command = [uv, "pip", "install", "--python", str(python), "--native-tls", "-e", ".[dev]"]
    if run(command, env=env) == 0:
        return True
    warn("uv install failed; falling back to pip")
    return False


def install_with_pip(python: Path) -> None:
    step("Installing task-graph and dev dependencies (pip)")
    if run([str(python), "-m", "pip", "install", "-e", ".[dev]"]) != 0:
        raise SystemExit("dependency installation failed")


def is_installed(python: Path) -> bool:
    """Report whether ``task_graph`` is importable from the virtualenv."""
    if not python.exists():
        return False
    probe = subprocess.run(
        [str(python), "-c", "import task_graph"],
        capture_output=True,
        cwd=str(ROOT),
        check=False,
    )
    return probe.returncode == 0


def ensure_environment(*, force: bool = False) -> Path:
    """Make ``.venv`` exist and hold an installed task-graph. Idempotent.

    The Run script calls this too, so pressing Run on a session whose setup was
    skipped provisions it instead of failing on a missing interpreter.
    """
    python = ensure_venv()
    if not force and is_installed(python):
        step("Dependencies already installed")
        return python

    index_url = discover_index_url(python)
    if index_url:
        step(f"Using package index {index_url}")
    if not install_with_uv(python, index_url):
        install_with_pip(python)
    if not is_installed(python):
        raise SystemExit("install reported success but task_graph is not importable")
    return python


def main() -> int:
    python = ensure_environment(force="--force" in sys.argv[1:])
    version = subprocess.run(
        [str(python), "-c", "import task_graph; print(task_graph.__version__)"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    ).stdout.strip()
    step(f"Ready: task-graph {version}".rstrip())
    print("Press Run for an interactive tg shell, or use: .\\run.ps1 shell", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
