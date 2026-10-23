# Pre-mark verification in Acrobat Pro

`premark.py` writes `/Redact` annotations into the volume PDFs. They are meant
to open in Acrobat’s Redact tool as marked but not applied, so a reviewer can
review them and click Apply. The automated tests cannot run Acrobat, so this
check has to be done by hand. It takes about ten minutes.

## What to open

`docs/samples/premark-sample.pdf`, a three-page fictional memo that has
already been pre-marked with `docs/samples/premark-terms.txt`. Regenerate it
after any change to `premark.py` with:

```
PYTHONPATH=src .venv/bin/python docs/samples/make_premark_sample.py
```

Expected marks per page (pages 1 and 2 are identical apart from rotation):

| Category | Count | Text |
| --- | --- | --- |
| student | 6 | Jane Doe (twice), Doe, Jane, J. Doe, Doe, JANE DOE |
| parent | 1 | Robert Doe |
| phone | 2 | (612) 555-0142, 612-555-0199 |
| dob | 1 | 03/14/2015 (not the “DOB:” label) |
| ssn | 1 | 123-45-6789 |

Page 3 has no marks. “Janet Doerr” and the lone word “Jane” must not be marked.

## Checklist

Work on a copy of the sample so the original stays reusable.

1. **Marks appear.** Open the file. Tools > Redact (or Protect > Redact).
   Every hit above should show a red outline. Hovering shows the black fill
   with the overlay text. The Comments panel lists 22 “Redact” entries, author
   `pst2pdf`. Note whether Acrobat shows the marks by default or only after
   the Redact tool is opened.
2. **Rotated page.** Page 2 carries `/Rotate 90`. The outlines must sit on the
   same words as on page 1, not shifted, mirrored, or off the page.
3. **Properties.** Right-click a student mark > Properties (or the Redaction
   Properties in the Redact toolbar). Confirm:
   * Overlay text (Custom Text) reads `FERPA`.
   * Phone, dob, and ssn marks carry their category as overlay text.
   * Fill color after applying is black.
   * The mark’s text is the category name (Contents), author `pst2pdf`.
4. **Apply honors the overlay.** Click Apply (all marks). Every box should be
   solid black with the overlay text drawn over it. Record how the text looks:
   `premark.py` asks for auto-sized white text (`/DA (/Helv 0 Tf 1 g)`,
   centered). If Acrobat ignores that and draws red left-aligned text, or draws
   nothing, note it so the DA string can be changed.
5. **Applied text is gone.** After Apply, select all text on page 1 (Ctrl/Cmd+A,
   copy, paste into a text editor). None of the marked strings should remain.
   Also try Find (Ctrl/Cmd+F) for `Doe`, `555`, `2015`, `6789`: no results.
6. **Sanitize order.** On a fresh copy, run Sanitize Document (Protect >
   Redact > Sanitize Document, or the “Remove Hidden Information” step)
   *before* Apply. Then check whether the marks survive. Acrobat’s sanitizer
   removes comments and annotations, so the expectation is that unapplied
   marks are stripped. Whatever the result, record it: it fixes the order of
   the steps on the START HERE page (Apply first, then Sanitize). If the marks
   do survive Sanitize, that is also worth writing down.
7. **Save round-trip.** After Apply and Sanitize, Save As a new file and reopen
   it. Confirm there are no comments left and the black boxes print (File >
   Print > preview).
8. **Non-Acrobat viewers (optional).** Open the unapplied sample in Preview or
   a browser. A red outline should show on each hit (the annotation carries a
   minimal appearance stream). Nothing is removed by these viewers, which is
   expected. Only Acrobat applies redactions.

## What to report

If you run the check, please open an issue with:

* Which of the eight steps passed.
* A screenshot of step 4 (the applied overlay text).
* The Sanitize-before-Apply result from step 6.
* Anything Acrobat complained about on open (“the file has been modified”,
  repair prompts, a warning about an incremental update). The annotations are
  written as an incremental update appended to the original file. The page
  content is never rewritten.

## Known uncertainties

* The `/DA` string (font, auto-size, color) is the one thing Acrobat may
  interpret differently from the intent. Everything else in the annotation
  follows the PDF 1.7 specification for `/Redact`.
* QuadPoints are written in Acrobat’s order (upper-left, upper-right,
  lower-left, lower-right), which differs from the wording of the spec. If
  marks look right but Apply blacks out a different region, this is the
  first thing to change.
* Acrobat should regenerate the appearance of the mark itself. If it keeps
  the shipped thin red outline instead of its own style, that is cosmetic.
