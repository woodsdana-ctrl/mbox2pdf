# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Bump when the system packages, entrypoint or code change in a way an
# existing image must not miss: pst2pdf.sh rebuilds any image with a lower
# revision.
LABEL pst2pdf.image-revision="8"

# pst-utils: readpst. Pango/Cairo/GDK-Pixbuf: WeasyPrint. LibreOffice (no GUI
# variants): Office-format attachment conversion. Fonts: DejaVu for the
# message template, Liberation for metric-compatible Office rendering, Noto
# Sans CJK for the Chinese, Japanese and Korean DejaVu has no glyphs for
# (without it they were drawn as empty boxes). Its serif faces are deleted:
# nothing asks for them, and they are 53 of its 91 MB. Noto Color Emoji for
# emoji, reached by font fallback only (see _base.html); WeasyPrint draws each
# one as a small image over an outline-free glyph that keeps it in the text
# layer.
#
# The font cache is built last, over font directories set to a whole-second
# mtime. fontconfig trusts a cache only while the directory's mtime matches
# the one it recorded, to the nanosecond, and an image layer keeps whole
# seconds: the cache fc-cache wrote was stale in every container, so each
# render process rescanned every font (40 MB of CJK among them) and tried to
# write its own cache under $HOME, and processes starting together raced to
# create that directory ("Fontconfig error: No writable cache directories").
# tini: see ENTRYPOINT.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pst-utils \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz-subset0 \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        libreoffice-impress-nogui \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        tzdata \
        tini \
    && rm -f /usr/share/fonts/opentype/noto/NotoSerifCJK-*.ttc \
    && find /usr/share/fonts /usr/local/share/fonts -type d -exec touch -d "@$(date +%s)" {} + \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir .; \
    else pip install --no-cache-dir .; fi

# Run as an unprivileged user by default; the wrapper still passes --user so
# output files are owned by the invoking account.
RUN useradd --create-home --uid 1000 pst2pdf
USER pst2pdf
ENV HOME=/home/pst2pdf

# tini, not pst2pdf, is PID 1. Opening an Office document makes LibreOffice
# probe for a signing backend, which forks gpgconf/gpg/gpgsm; those exit while
# soffice.bin is still running, and soffice.bin itself outlives the oosplash
# launcher that convert.py waits on. Every one of them is reparented to PID 1,
# and Python reaps only the children it knows about, so without an init they
# accumulate as zombies -- five per Office attachment -- until the container
# hits its process cap and thread creation starts failing (Pango turns that
# into a fatal GLib error). PID 1 also ignores signals it has no handler for,
# so that abort would hang the run instead of ending it. tini reaps the
# orphans and gives pst2pdf ordinary signal semantics.
ENTRYPOINT ["/usr/bin/tini", "--", "pst2pdf"]
