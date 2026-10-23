"""Tests for pst2pdf.sh, the bash front door.

The wrapper is exercised for real with a fake ``docker`` on PATH that
records the ``run`` command line it was handed, so the container hardening
(file-only mounts, no capabilities, no network) is checked without a daemon.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "pst2pdf.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not WRAPPER.is_file(),
    reason="bash and the wrapper script are needed",
)


def test_wrapper_syntax():
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def _docker_shim(tmp_path: Path) -> tuple[Path, Path]:
    """A ``docker`` that answers info/inspect and records ``run`` argv, one per line."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "docker-run.txt"
    record.unlink(missing_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = run ]; then printf '%s\\n' \"$@\" > '{record}'; fi\n"
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return bin_dir, record


def _run_wrapper(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir, record = _docker_shim(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),  # a scratch home, so the run never touches the real one
    }
    proc = subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    argv = record.read_text().splitlines() if record.exists() else []
    return proc, argv


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    return list(zip(argv, argv[1:]))


def _mounts(argv: list[str]) -> list[str]:
    """The ``-v`` bind mounts, i.e. docker options before the image name (the
    container's own ``-v`` verbose flag comes after it)."""
    docker_opts = argv[: argv.index("pst2pdf")]
    return [value for flag, value in _pairs(docker_opts) if flag == "-v"]


def test_wrapper_mounts_only_the_named_files_and_hardens_the_container(tmp_path):
    archive = tmp_path / "in" / "Sample archive.pst"
    archive.parent.mkdir()
    archive.write_bytes(b"!BDN" + b"\x00" * 60)
    (archive.parent / "unrelated.txt").write_text("must not be mounted")
    terms = tmp_path / "terms" / "names.txt"
    terms.parent.mkdir()
    terms.write_text("[student] Jane Doe\n")
    out = tmp_path / "out"

    proc, argv = _run_wrapper(
        tmp_path, "-v", "-o", str(out), "--terms", str(terms), "--jobs", "3", str(archive), "Request 7"
    )
    assert proc.returncode == 0, proc.stderr
    assert argv and argv[0] == "run", argv

    pairs = _pairs(argv)
    for flag, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", "512"),
        ("--jobs", "3"),
        ("--request-id", "Request 7"),
        ("--terms", "/terms/names.txt"),
    ):
        assert (flag, value) in pairs, (flag, value, argv)
    assert "--rm" in argv

    mounts = _mounts(argv)
    assert f"{archive.resolve()}:/in/Sample archive.pst:ro" in mounts
    assert f"{terms.resolve()}:/terms/names.txt:ro" in mounts
    assert f"{out.resolve()}:/out" in mounts
    assert len(mounts) == 3
    # Never a directory: the archive's and the terms file's parents stay outside.
    for value in mounts:
        host = value.split(":", 1)[0]
        assert Path(host).is_file() or Path(host) == out.resolve(), value
    assert not any(v.startswith(f"{archive.parent.resolve()}:") for v in mounts)

    assert argv[-2:] == ["/in/Sample archive.pst", "/out"]
    assert argv[argv.index("pst2pdf") + 1] == "-v"


def test_wrapper_without_terms_mounts_archive_and_output_only(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, str(archive))
    assert proc.returncode == 0, proc.stderr
    assert _mounts(argv) == [f"{archive.resolve()}:/in/a.pst:ro", f"{(tmp_path / 'a-pdfs').resolve()}:/out"]
    assert "--terms" not in argv
    assert ("--cap-drop", "ALL") in _pairs(argv)


def test_wrapper_refuses_missing_inputs(tmp_path):
    proc, argv = _run_wrapper(tmp_path, str(tmp_path / "missing.pst"))
    assert proc.returncode == 1 and "not found" in proc.stderr
    assert argv == []
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--terms", str(tmp_path / "no-terms.txt"), str(archive))
    assert proc.returncode == 1 and "terms file not found" in proc.stderr
    assert argv == []


def test_wrapper_forwards_appendix_over(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--appendix-over", "0", str(archive))
    assert proc.returncode == 0, proc.stderr
    assert ("--appendix-over", "0") in _pairs(argv)
    # Container flags come after the image name.
    assert argv.index("--appendix-over") > argv.index("pst2pdf")


@pytest.mark.parametrize("value", ["-1", "five", ""])
def test_wrapper_rejects_a_bad_appendix_value(tmp_path, value):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--appendix-over", value, str(archive))
    assert proc.returncode == 1 and "--appendix-over needs a whole number" in proc.stderr
    assert argv == []


def test_wrapper_says_separate_was_removed(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--separate", str(archive))
    assert proc.returncode == 1 and "--separate was removed" in proc.stderr and "--triage" in proc.stderr
    assert argv == []


def test_wrapper_help_mentions_appendix(tmp_path):
    proc, _ = _run_wrapper(tmp_path, "--help")
    assert proc.returncode == 0
    assert "--appendix-over N" in proc.stdout and "Attachments 01 of 02.pdf" in proc.stdout


# --- the container's init ---------------------------------------------------
#
# LibreOffice forks gpgconf/gpg/gpgsm when it opens an Office document and
# leaves them, plus soffice.bin itself, orphaned. Whatever is PID 1 inherits
# them, and Python reaps only its own children, so with pst2pdf as PID 1 five
# zombies survived every Office attachment until --pids-limit was exhausted
# and thread creation began to fail mid-run.


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def test_image_runs_an_init_as_pid1():
    text = _dockerfile()
    entrypoint = [line for line in text.splitlines() if line.startswith("ENTRYPOINT")]
    assert entrypoint == ['ENTRYPOINT ["/usr/bin/tini", "--", "pst2pdf"]'], entrypoint
    # ... and the init is actually installed.
    assert "\n        tini \\\n" in text


def _shim_with_revision(tmp_path: Path, revision: str, source: str | None = None) -> tuple[Path, Path, Path]:
    """A ``docker`` whose ``image inspect --format`` answers *revision* for the
    image's pst2pdf.image-revision label and *source* for its pst2pdf.source
    label (default: the current fingerprint, i.e. built from this checkout),
    and that records ``build``'s arguments, one per line."""
    if source is None:
        source = _source_fingerprint(ROOT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    built = tmp_path / "docker-build.txt"
    record = tmp_path / "docker-run.txt"
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = build ]; then printf '%s\\n' \"$@\" > '{built}'; fi\n"
        f"if [ \"$1\" = run ]; then printf '%s\\n' \"$@\" > '{record}'; fi\n"
        f"if [ \"$1\" = image ]; then case \"$*\" in *image-revision*) printf '%s\\n' '{revision}';; *pst2pdf.source*) printf '%s\\n' '{source}';; esac; fi\n"
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return bin_dir, built, record


def _source_fingerprint(root: Path) -> str:
    """What pst2pdf.sh's source_fingerprint computes, done independently: the
    SHA-256 of "<sha256>  <path>" lines for the Dockerfile, pyproject.toml and
    every file under src/ except bytecode and .DS_Store, sorted by path."""
    files = ["Dockerfile", "pyproject.toml"] + [
        p.relative_to(root).as_posix()
        for p in (root / "src").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store"
    ]
    lines = "".join(f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}\n" for f in sorted(files))
    return hashlib.sha256(lines.encode()).hexdigest()


def _image_revision() -> int:
    (line,) = [line for line in _dockerfile().splitlines() if line.startswith("LABEL pst2pdf.image-revision=")]
    return int(line.split("=", 1)[1].strip('"'))


def test_image_has_the_cjk_and_emoji_fonts_and_a_revision_that_says_so():
    text = _dockerfile()
    assert "\n        fonts-noto-cjk \\\n" in text
    assert "\n        fonts-noto-color-emoji \\\n" in text
    assert _image_revision() >= 5  # 1: tini, 2: fonts-noto-cjk, 3: fonts-noto-color-emoji, 4: a valid font cache, 5: stable readpst .ics


@pytest.mark.parametrize(
    ("label", "rebuilds"),
    [("current", False), ("1", True), ("<no value>", True)],  # "<no value>": built before the label
)
def test_wrapper_rebuilds_an_image_older_than_the_dockerfile(tmp_path, label, rebuilds):
    revision = str(_image_revision()) if label == "current" else label
    bin_dir, built, _ = _shim_with_revision(tmp_path, revision)
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc = subprocess.run(
        ["bash", str(WRAPPER), str(archive)],
        cwd=str(tmp_path),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert built.exists() is rebuilds, proc.stdout


def test_wrapper_mounts_the_decisions_file_alone_and_read_only(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    survey_dir = tmp_path / "survey out"
    survey_dir.mkdir()
    decisions = survey_dir / "Attachment survey.csv"
    decisions.write_text("filename,decision\n")
    (survey_dir / "manifest.json").write_text("{}")  # a neighbour that must stay outside
    proc, argv = _run_wrapper(tmp_path, "--decisions", str(decisions), str(archive))
    assert proc.returncode == 0, proc.stderr
    assert f"{decisions.resolve()}:/decisions/Attachment survey.csv:ro" in _mounts(argv)
    assert ("--decisions", "/decisions/Attachment survey.csv") in _pairs(argv)
    proc, argv = _run_wrapper(tmp_path, "--decisions", str(tmp_path / "nope.csv"), str(archive))
    assert proc.returncode == 1 and "decisions file not found" in proc.stderr


def test_wrapper_mounts_only_the_summary_of_a_search_report_folder(tmp_path):
    """Safari unzips the Reports zip into a folder that also holds the
    per-item report: only the Summary CSV may reach the container."""
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    folder = tmp_path / "Reports-Example export"
    folder.mkdir()
    summary = folder / "Summary-2021-03-04_13-02-03.csv"
    summary.write_text('"Query","example"\n')
    (folder / "Items_0_2021-03-04_13-02-03.csv").write_text("")
    proc, argv = _run_wrapper(tmp_path, "--search-report", str(folder), str(archive))
    assert proc.returncode == 0, proc.stderr
    mounts = [m for m in _mounts(argv) if "/search/" in m]
    assert mounts == [f"{summary.resolve()}:/search/{summary.name}:ro"]
    assert ("--search-report", f"/search/{summary.name}") in _pairs(argv)


def test_wrapper_mounts_a_search_report_zip_alone(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    zpath = tmp_path / "Reports-Example.zip"
    zpath.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    proc, argv = _run_wrapper(tmp_path, "--search-report", str(zpath), str(archive))
    assert proc.returncode == 0, proc.stderr
    assert f"{zpath.resolve()}:/search/Reports-Example.zip:ro" in _mounts(argv)


def test_wrapper_refuses_a_search_report_it_cannot_use(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    empty = tmp_path / "Reports-empty"
    empty.mkdir()
    proc, _ = _run_wrapper(tmp_path, "--search-report", str(empty), str(archive))
    assert proc.returncode == 1 and "expected one Summary" in proc.stderr
    proc, _ = _run_wrapper(tmp_path, "--search-report", str(tmp_path / "nope.zip"), str(archive))
    assert proc.returncode == 1 and "search report not found" in proc.stderr


@pytest.mark.parametrize("shift", [3, 25, 60])
def test_editing_the_wrapper_during_a_run_does_not_break_its_wrap_up(tmp_path, shift):
    """bash reads a script as it runs it. The wrapper was rewritten in place
    while a conversion ran, and the wrap-up was read from a stale offset:
    "syntax error near unexpected token `fi'". Its body is now one function,
    parsed whole before the conversion starts. (Replacing the file, as git
    and mv do, never mattered: bash keeps reading the old one.) Before the
    fix, nearly every shift from 3 to 60 bytes broke the run."""
    script_dir = tmp_path / "repo"
    script_dir.mkdir()
    wrapper = script_dir / "pst2pdf.sh"
    shutil.copy(WRAPPER, wrapper)
    shutil.copy(ROOT / "Dockerfile", script_dir / "Dockerfile")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    # `run` stands in for the conversion: while it "runs", *shift* bytes are
    # inserted near the top of the wrapper, written over the same file.
    shim.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = image ]; then case \"$*\" in *--format*) echo {_image_revision()};; esac; fi\n"
        "if [ \"$1\" = run ]; then\n"
        f"  {{ head -c 40 '{wrapper}'; printf '%{shift}s' '' | tr ' ' '#'; tail -c +41 '{wrapper}'; }} > '{tmp_path}/edited'\n"
        f"  cat '{tmp_path}/edited' > '{wrapper}'\n"
        "fi\n"
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc = subprocess.run(
        ["bash", str(wrapper), str(archive)],
        cwd=str(tmp_path),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "#" * shift in wrapper.read_text()  # the edit really happened
    assert proc.returncode == 0, proc.stderr
    assert "syntax error" not in proc.stderr
    assert "==> Done" in proc.stdout


def test_wrapper_passes_triage_and_points_at_the_index(tmp_path):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--triage", str(archive))
    assert proc.returncode == 0, proc.stderr
    assert "--triage" in argv
    assert "Preparing a triage copy" in proc.stdout and "Triage done" in proc.stdout
    out_dir = (tmp_path / "a-pdfs").resolve()
    assert f"Reviewers fill in: {out_dir}/Attachment index - DO NOT RELEASE.csv" in proc.stdout


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--survey",), "--triage and --survey are different runs"),
        (("--appendix-over", "5"), "--appendix-over cannot be combined with --triage"),
    ],
)
def test_wrapper_refuses_triage_with_what_it_cannot_do(tmp_path, extra, message):
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc, argv = _run_wrapper(tmp_path, "--triage", *extra, str(archive))
    assert proc.returncode == 1 and message in proc.stderr
    assert argv == []


def _run_wrapper_writing(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Like _run_wrapper, but the fake container writes a volume and an
    originals folder into the output directory, so --zip has something to pack."""
    bin_dir, _ = _docker_shim(tmp_path)
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = run ]; then\n"
        "  for a in \"$@\"; do case \"$a\" in *:/out) out=${a%:/out} ;; esac; done\n"
        "  mkdir -p \"$out/Original attachments - DO NOT RELEASE\"\n"
        "  echo pdf > \"$out/Volume 01 of 01.pdf\"\n"
        "  echo original > \"$out/Original attachments - DO NOT RELEASE/A0001 plan.txt\"\n"
        "fi\n"
        "exit 0\n"
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)}
    return subprocess.run(["bash", str(WRAPPER), *args], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("zip") is None, reason="zip is needed")
def test_wrapper_zip_leaves_the_originals_out_except_for_triage(tmp_path):
    import zipfile

    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc = _run_wrapper_writing(tmp_path, "--zip", "--keep-natives", str(archive))
    assert proc.returncode == 0, proc.stderr
    names = zipfile.ZipFile(tmp_path / "a-pdfs.zip").namelist()
    assert "a-pdfs/Volume 01 of 01.pdf" in names
    assert not any("Original attachments" in n for n in names)
    assert "was left out of the zip: it is not redacted" in proc.stdout

    proc = _run_wrapper_writing(tmp_path, "--zip", "--triage", "-f", str(archive))
    assert proc.returncode == 0, proc.stderr
    names = zipfile.ZipFile(tmp_path / "a-pdfs - triage - DO NOT RELEASE.zip").namelist()
    assert "a-pdfs/Original attachments - DO NOT RELEASE/A0001 plan.txt" in names


def _run_with_image(tmp_path: Path, wrapper: Path, revision: str, source: str) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir, built, _ = _shim_with_revision(tmp_path, revision, source)
    built.unlink(missing_ok=True)
    archive = tmp_path / "a.pst"
    archive.write_bytes(b"!BDN")
    proc = subprocess.run(
        ["bash", str(wrapper), "-f", "-o", str(tmp_path / "out"), str(archive)],
        cwd=str(tmp_path),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc, built


def test_wrapper_rebuilds_an_image_built_from_other_code(tmp_path):
    """After a git pull or a fresh download the image still holds the old
    code: the fingerprint says so and the wrapper rebuilds without being asked,
    labelling the new image with the current fingerprint."""
    current = _source_fingerprint(ROOT)
    proc, built = _run_with_image(tmp_path, WRAPPER, str(_image_revision()), "0" * 64)
    assert built.exists(), proc.stdout
    assert "pst2pdf has changed since its Docker image was built" in proc.stdout
    args = built.read_text().splitlines()
    assert ("--label", f"pst2pdf.source={current}") in list(zip(args, args[1:]))
    proc, built = _run_with_image(tmp_path, WRAPPER, str(_image_revision()), "<no value>")  # built by hand
    assert built.exists()
    proc, built = _run_with_image(tmp_path, WRAPPER, str(_image_revision()), current)
    assert not built.exists() and "rebuilding" not in proc.stdout


def test_fingerprint_ignores_bytecode_and_notices_code_changes(tmp_path):
    """Run from a copy of the checkout, so its src/ can be changed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("pst2pdf.sh", "Dockerfile", "pyproject.toml"):
        shutil.copy2(ROOT / name, repo / name)
    shutil.copytree(ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    wrapper, revision = repo / "pst2pdf.sh", str(_image_revision())
    fingerprint = _source_fingerprint(repo)
    assert fingerprint == _source_fingerprint(ROOT)  # the copy is faithful

    cache = repo / "src" / "pst2pdf" / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-312.pyc").write_bytes(b"left behind by a test run")
    (repo / "src" / ".DS_Store").write_bytes(b"Finder")
    (repo / "README.md").write_text("edited docs only")
    _, built = _run_with_image(tmp_path, wrapper, revision, fingerprint)
    assert not built.exists()

    with (repo / "src" / "pst2pdf" / "cli.py").open("a") as fh:
        fh.write("\n# an update\n")
    proc, built = _run_with_image(tmp_path, wrapper, revision, fingerprint)
    assert built.exists() and "pst2pdf has changed" in proc.stdout
    args = built.read_text().splitlines()
    assert ("--label", f"pst2pdf.source={_source_fingerprint(repo)}") in list(zip(args, args[1:]))


def test_wrapper_mbox_directory_mount_and_timezone(tmp_path):
    source=tmp_path/'exports';source.mkdir();(source/'a.mbox').write_bytes(b'')
    proc,argv=_run_wrapper(tmp_path,'--input-format','mbox','--display-timezone','America/Chicago',str(source))
    assert proc.returncode==0,proc.stderr
    assert f'{source.resolve()}:/in/exports:ro' in _mounts(argv)
    assert ('--display-timezone','America/Chicago') in _pairs(argv)
    assert ('--network','none') in _pairs(argv)


def test_wrapper_rejects_output_inside_mbox_directory(tmp_path):
    source=tmp_path/'exports';source.mkdir()
    proc,argv=_run_wrapper(tmp_path,'-o',str(source/'output'),str(source))
    assert proc.returncode!=0 and 'outside' in proc.stderr
    assert not argv


@pytest.mark.skipif(shutil.which('zip') is None,reason='zip is needed')
def test_production_zip_excludes_sensitive_reports(tmp_path):
    import zipfile
    archive=tmp_path/'a.pst';archive.write_bytes(b'!BDN')
    out=tmp_path/'a-pdfs';out.mkdir()
    for name in ['manifest.json','Intake report - DO NOT RELEASE.json','00 START HERE.pdf','Redaction word list.txt','Pre-marked terms - DO NOT RELEASE.txt']:
        (out/name).write_text('sensitive synthetic test')
    proc=_run_wrapper_writing(tmp_path,'--zip','-f',str(archive))
    assert proc.returncode==0,proc.stderr
    names=zipfile.ZipFile(tmp_path/'a-pdfs.zip').namelist()
    assert any('Volume' in n for n in names)
    assert not any('manifest' in n or 'DO NOT RELEASE' in n or 'START HERE' in n or 'word list' in n for n in names)
