"""Check release wheels in isolation, optionally fetching members from a published index."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def wheel_requirement(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_path, = (
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    name, version = metadata["Name"], metadata["Version"]
    if not name or not version:
        raise ValueError(f"Missing Name or Version in {wheel}")
    return f"{name}=={version}"


def check_release(dist: Path, index_url: str | None = None) -> None:
    dist = dist.resolve()
    umbrella, = sorted((dist / "umbrella").glob("*.whl"))
    members = sorted((dist / "members").glob("*.whl"))
    if not members:
        raise ValueError(f"No member wheels in {dist / 'members'}")

    smoke_script = Path(__file__).with_name("smoke_quickstart.py").resolve()
    with tempfile.TemporaryDirectory(prefix="auryn-release-") as directory:
        work = Path(directory)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(
            AURYN_HOME=str(work / "home"),
            AURYN_CACHE_HOME=str(work / "cache"),
            AURYN_AUTO_INSTALL="1",
        )

        def run(*command: str) -> None:
            subprocess.run(command, cwd=work, env=env, check=True)

        run(sys.executable, "-m", "venv", str(work / "venv"))
        python = str(work / "venv" / "bin" / "python")
        requirements = work / "members.txt"
        requirements.write_text("".join(f"{wheel_requirement(wheel)}\n" for wheel in members))
        wheelhouse = dist / "members"
        if index_url:
            wheelhouse = work / "published"
            # Never fall back to local members or PyPI when verifying TestPyPI availability.
            run(
                python, "-m", "pip", "--isolated", "download", "--no-cache-dir",
                "--no-deps", "--only-binary=:all:", "--index-url", index_url,
                "--dest", str(wheelhouse), "-r", str(requirements),
            )
        run(
            python, "-m", "pip", "--isolated", "install",
            "--index-url", "https://pypi.org/simple/",
            "--find-links", str(wheelhouse), "--constraint", str(requirements),
            str(umbrella),
        )
        run(python, "-m", "pip", "--isolated", "check")
        run(str(work / "venv" / "bin" / "auryn"), "--help")
        run(python, str(smoke_script))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("--index-url", help="Download every exact member version from this index first.")
    args = parser.parse_args()
    check_release(args.dist, args.index_url)
