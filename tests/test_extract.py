import subprocess

from pst2pdf import extract


def test_readpst_runs_serially(tmp_path, monkeypatch):
    # Only -j 0 is serial. -j 1 still forks per folder and drops messages.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(extract.shutil, "which", lambda name: "/usr/bin/readpst")
    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    extract.extract_pst(tmp_path / "archive.pst", tmp_path / "work")

    (cmd,) = calls
    assert cmd[cmd.index("-j") + 1] == "0"
