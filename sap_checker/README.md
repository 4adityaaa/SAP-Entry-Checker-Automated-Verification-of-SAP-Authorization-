# SAP Entry Checker

Upload up to **40 SAP screenshots** (max **15 MB each**) and the **master Excel**.
The tool reads each screenshot with OCR + OpenCV, extracts only the authorization
objects / fields / values that appear in your Excel, and marks every Excel row **PASS** or **FAIL**.

## Run in VS Code (Windows)

1. Install Python 3.10 - 3.12 (Python 3.12 is recommended; tick "Add Python to PATH"). Open this folder in VS Code.
2. Open a PowerShell terminal (Ctrl + `). If `.venv` was already created with another Python version, remove that environment first:

```
Remove-Item -Recurse -Force .venv
```

Then create and activate the Python 3.12 environment and install dependencies:

```
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

For Python 3.10 or 3.11, replace `py -3.12` with `py -3.10` or `py -3.11`.
macOS / Linux: use `python3 -m venv .venv` and `source .venv/bin/activate`.

3. The browser opens at http://127.0.0.1:5000 (if not, open it yourself).
4. Upload screenshots + Excel, press **Check entries**.
   For a privacy-safe test, generate synthetic inputs as described below.

No Tesseract or SAP system is needed. On Windows, the requirements install the
local Microsoft C++ runtime used by RapidOCR's native dependencies. The OCR
model may need an internet connection on its first initialization, depending on
the RapidOCR version.

## Generate synthetic demo data

Do not use client screenshots during development. Generate a fictional SUIM-style
authorization screen and its matching workbook with:

```
python tools\generate_demo_data.py
```

Upload `sample_data\synthetic_demo\demo_suim.png` and
`sample_data\synthetic_demo\demo_master.xlsx` in the app. In strict mode, the
workbook is designed to produce **1 PASS and 2 FAIL** results: one complete
match, one missing transaction value, and one missing authorization object.
These images target the authorization-object/field/value criteria this checker
extracts; they contain no real SAP or client information.

## How it works

| Step | File | What happens |
|---|---|---|
| 1 | `core/excel_parser.py` | Reads each row: objects, fields, values, OR/AND logic such as `(01 OR 02) AND 07` |
| 2 | `core/ocr_engine.py` | OpenCV: fixes phone rotation, resizes, sharpens blurry images, **deskews** tilted photos; RapidOCR reads text |
| 3 | `core/extractor.py` | Rebuilds the SUIM layout and keeps only workbook-relevant authorization values |
| 4 | `core/matcher.py` | Compares every Excel row (tolerates OCR slips like `O2` vs `02`, `STCODE` vs `S_TCODE`) and tracks screenshot evidence |
| 5 | `core/pipeline.py` | Runs everything, writes the Excel report |

## Match modes
* **Strict (default):** every value written in the Excel must be visible on the screen.
* **Lenient:** Excel logic is evaluated (`SU01 OR SU10` passes if either is shown).

## Understanding the accuracy indicators

The result page reports **Criteria accuracy** as the percentage of workbook
object/field checks satisfied by the extracted screenshot data, plus the PASS
rate across complete Excel rows. It also shows the OCR engine's average
confidence for relevant detected text. OCR confidence is not measured
character-recognition accuracy. A 98% OCR accuracy target must be measured on a
separate set of manually transcribed screenshots that were not used to tune the
extractor; the provided screenshots do not contain those ground-truth labels.

## Tips for good accuracy
* Name files in order (`01.png`, `02.png` ...). If a SUIM screen is scrolled, the next screenshot's
  top part is joined to the previous screenshot's last block.
* A row passes only if **all** its authorization objects are found (can be on different screenshots,
  the evidence column tells you which).
* Sharp, full-width screenshots work best. Warnings (blur, low resolution) show under each screenshot.
* Check the **Screenshots & extracted data** tab: green boxes = values the OCR used.

## Troubleshooting
* **No OCR engine / RapidOCR dependency error:** use Python 3.10-3.12. If `.venv` was created with another Python version, remove and recreate it as shown in the setup steps. Always install packages and run `app.py` from the same activated environment. On Windows, rerun `python -m pip install -r requirements.txt` to install the app-local MSVC runtime.
* **Port busy (PowerShell):** `$env:PORT = "5055"; python app.py`.
