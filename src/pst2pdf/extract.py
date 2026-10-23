"""Stage 1: explode a .pst into .eml files with readpst (libpst)."""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    pass


def extract_pst(pst_path: Path, work_dir: Path) -> Path:
    """Run readpst against *pst_path*, writing .eml files under *work_dir*.

    readpst recreates the PST folder hierarchy as directories, which is what
    the rest of the pipeline mirrors into the output tree. Returns *work_dir*.
    """
    if shutil.which("readpst") is None:
        raise ExtractError(
            "readpst not found on PATH (install libpst: 'apt-get install pst-utils' "
            "or 'brew install libpst')"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    # -j 0 is load-bearing: it is readpst's only serial mode. Any -j N with
    # N >= 1, including -j 1, still forks a child per folder, and the
    # children race on output file numbering and silently drop a random
    # subset of messages (-j 1 lost a few messages of a test archive, a
    # different few each run; -j 0 never lost one).
    cmd = ["readpst", "-j", "0", "-e", "-8", "-o", str(work_dir), str(pst_path)]
    log.info("extracting: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ExtractError(
            f"readpst exited {result.returncode}: {result.stderr.strip()[-2000:]}"
        )
    return work_dir
