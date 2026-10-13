# LungLogic posters

The current, restructured poster is `LungLogic_Poster_Integrated.*`:

- `LungLogic_Poster_Integrated.pptx` — editable A0 poster.
- `LungLogic_Poster_Integrated.pdf` — print version.
- `LungLogic_Poster_Integrated.html` — browser version.
- `LungLogic_Poster_Integrated_preview.png` — full-page preview.
- `build_restructured_poster.py` — build script for these files.

It adds a sourced project introduction and a conceptual breathing-cycle plot,
defines RR, I:E, tidal volume and repeatability with a nominal-values note,
and frames the project around the unmet need for programmable breathing timing,
repeatable volume and later independent left/right actuation. Software and
control are combined in Section 3. Section 4 reserves a large area for measured
experimental results and sets out the path to integration with the medical
biomimetic lung model. The original
six-lobe photograph has been removed from the poster: the team's prototype
photograph has an explicit placeholder. The hardware section shows one
bridge-board photograph. The control diagram uses the requested teal, orange
and blue palette with different block widths.

The A0 typography is fixed at 84 pt for the two-line title, 36 pt for author
details, 48 pt for primary section headings, 36 pt for body/table/caption text,
and 18 pt for source/footer text. The validation figure contains Volume and
Flow tracking only; the PPV-command subplot was removed.

The standalone `Software operation` paragraph was removed in the latest layout.
The remaining copy is shortened, the User/Developer screenshots and control
diagram are larger, and Auto PID plus delay compensation are summarized in one
full-width line.

Decorative frames around inserted figures were removed. Only the two deliberate
placeholders for the future prototype photograph and experimental results keep
their outlines.

`LungLogic_Poster_2026-10-02_single_branch.*` is the latest user-supplied-deck
variant. It includes the digital-twin volume/flow result, omits the control
formula strip, clarifies optional Auto PID and Smith prediction, and keeps
sections 2–4 explicitly scoped to one 250 mL branch. The original source and
intermediate poster files remain available.

The `LungLogic_Poster_Revised.*` files below are the earlier version.

Generated files:

- `LungLogic_Poster_Revised.pptx` — editable A0 portrait poster.
- `LungLogic_Poster_Revised.pdf` — print/export version.
- `LungLogic_Poster_Revised.html` — browser preview and print version.
- `LungLogic_Poster_Revised_preview.png` — quick visual preview.
- `build_revised_poster.py` — reproducible build script.

Key revisions:

- Reorganized all four sections as `OBJECTIVE → explanation → evidence`, using
  the visual structure of the numbered integrated-control presentation.
- Replaced the dense plant equations with a closed-loop control diagram. The
  two retained equations define the inspiratory flow reference and actual
  feedforward/PID valve command.
- Added a three-step measured-model transfer graphic: identify the physical
  path, pre-validate in the digital twin, then deploy and lock the parameters.
- Removed the A2, B2 and D placeholders and inserted project photographs and a
  current single-lung digital-twin validation figure.
- Corrected Auto PID to a bounded run-to-run Kp/Ki search; Kd and identified
  plant parameters remain fixed.
- Described Smith compensation using measured effective feedback delay and made
  clear that the hardware software does not insert artificial delay.
- Added the simulation-first parameter-identification workflow and default
  locking of accepted model/delay values.
- Distinguished model-based pre-validation from measured hardware evidence.

Before final submission, replace or supplement Figure 4 with controlled
hardware results if they are available. Recommended comparisons are PID versus
PID + Smith under identical conditions, and baseline versus selected Auto PID
gains with flow RMSE, tidal-volume error and PPV saturation reported.
