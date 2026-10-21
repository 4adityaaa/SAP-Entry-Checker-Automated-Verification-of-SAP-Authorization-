"""Focused tests for Excel parsing, OCR extraction, and row matching."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from openpyxl import Workbook

from core.excel_parser import (
    ObjectReq,
    RowReq,
    load_requirements,
    parse_expression,
    parse_fields,
    tree_eval,
)
from core.extractor import extract_blocks
from core.matcher import match_all
from core.pipeline import _merge_continuations, natural_key, run_job


def _ocr_items(lines):
    """Build predictable OCR geometry for a sequence of single-line results."""
    items = []
    for index, text in enumerate(lines):
        y = index * 40
        box = np.array(
            [[0, y], [600, y], [600, y + 24], [0, y + 24]],
            dtype=np.float32,
        )
        items.append((box, text, 0.99))
    return items


class ExpressionParserTests(unittest.TestCase):
    def test_and_precedes_or_and_parentheses_are_respected(self):
        tree = parse_expression("(01 OR 02) AND 07")
        self.assertTrue(tree_eval(tree, lambda value: value in {"02", "07"}))
        self.assertFalse(tree_eval(tree, lambda value: value in {"02"}))

    def test_quoted_wildcard_and_semicolon_separated_fields(self):
        fields = parse_fields("TABLE = '*'; ACTVT = 01 AND 02")
        self.assertEqual([field.name for field in fields], ["TABLE", "ACTVT"])
        self.assertEqual(fields[0].values, ["*"])
        self.assertEqual(fields[1].values, ["01", "02"])

    def test_malformed_expressions_raise_instead_of_being_silently_accepted(self):
        for expression in ("01 OR", "(01 AND 02", "()", "AND 01", "01 02"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    parse_expression(expression)


class ExtractionAndMatchingTests(unittest.TestCase):
    def test_synthetic_suim_layout_matches_expected_pass_and_fail_rows(self):
        lines = [
            "Authorization Object S_TCODE",
            "TCD - Transaction Code",
            "Value SU01 OR SU10",
            "Authorization Object S_USER_GRP",
            "ACTVT - Activity",
            "Value 01",
            "AND 02",
        ]
        objects = {"S_TCODE", "S_USER_GRP", "S_USER_AGR"}
        fields = {"TCD", "ACTVT"}
        extracted = extract_blocks(
            _ocr_items(lines + ["Execute All names are fictional"]),
            objects,
            fields,
            {"SU01", "SU10", "SU99", "01", "02"},
        )
        self.assertEqual(
            extracted["blocks"][1]["fields"]["ACTVT"], ["01", "02"]
        )
        requirements = [
            parse_fields("TCD = SU01 OR SU10"),
            parse_fields("TCD = SU99"),
            parse_fields("ACTVT = 02"),
        ]
        rows = []
        for index, (object_name, field_req) in enumerate(
            zip(("S_TCODE", "S_TCODE", "S_USER_AGR"), requirements), start=1
        ):
            rows.append(RowReq(index + 1, f"DEMO-{index}", "", [
                ObjectReq(object_name, field_req)
            ]))
        results = match_all(
            rows, [{"filename": "synthetic.png", "blocks": extracted["blocks"]}],
            strict=True,
        )
        self.assertEqual([result["status"] for result in results],
                         ["PASS", "FAIL", "FAIL"])

    def test_value_filter_uses_current_field_vocabulary(self):
        extracted = extract_blocks(
            _ocr_items([
                "Authorization Object S_TCODE",
                "TCD - Transaction Code",
                "Value SU01 02",
                "ACTVT - Activity",
                "Value 02",
            ]),
            {"S_TCODE"},
            {"TCD", "ACTVT"},
            {"SU01", "02"},
            {"TCD": {"SU01"}, "ACTVT": {"02"}},
        )
        self.assertEqual(
            extracted["blocks"][0]["fields"],
            {"TCD": ["SU01"], "ACTVT": ["02"]},
        )

    def test_unknown_authorization_objects_are_not_misread_as_continuations(self):
        extracted = extract_blocks(
            _ocr_items([
                "Authorization Object S_UNKNOWN",
                "ACTVT - Activity",
                "Value 02",
                "Authorization Object S_TCODE",
                "TCD - Transaction Code",
                "Value SU01",
            ]),
            {"S_TCODE"},
            {"ACTVT", "TCD"},
            {"02", "SU01"},
            {"ACTVT": {"02"}, "TCD": {"SU01"}},
        )
        self.assertEqual(
            extracted["blocks"],
            [{"object": "S_TCODE", "fields": {"TCD": ["SU01"]}}],
        )

    def test_unknown_field_header_and_desktop_footer_values_are_ignored(self):
        extracted = extract_blocks(
            _ocr_items([
                "Authorization Object S_TCODE",
                "TCD - Transaction Code",
                "Value SU01",
                "UNRELATED - Description",
                "Value SU10",
                "Document1 - Word",
                "Value SU10",
                "Type here to search DEBUG",
            ]),
            {"S_TCODE"},
            {"TCD"},
            {"SU01", "SU10"},
            {"TCD": {"SU01", "SU10"}},
        )
        self.assertEqual(
            extracted["blocks"],
            [{"object": "S_TCODE", "fields": {"TCD": ["SU01"]}}],
        )

    def test_continuation_evidence_lists_both_screenshots(self):
        screens = [
            {
                "filename": "01.png",
                "blocks": [{"object": "S_TCODE", "fields": {"TCD": ["SU01"]}}],
                "notes": [],
            },
            {
                "filename": "02.png",
                "blocks": [{"object": None, "fields": {"TCD": ["SU10"]}}],
                "notes": [],
            },
        ]
        _merge_continuations(screens)
        row = RowReq(2, "DEMO", "", [
            ObjectReq("S_TCODE", parse_fields("TCD = SU01 OR SU10"))
        ])
        result = match_all([row], screens, strict=True)[0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["evidence"], ["01.png", "02.png"])

    def test_natural_sort_supports_mixed_numeric_and_text_filenames(self):
        names = ["page10.png", "2.png", "page2.png", "image.png"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["2.png", "image.png", "page2.png", "page10.png"],
        )

    def test_workbook_requires_valid_expressions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["RAFIT", "Function", "Auth Object 1", "Auth Object 1 Fields"])
            sheet.append(["X", "Bad expression", "S_TCODE", "TCD = SU01 OR"])
            workbook.save(path)
            with self.assertRaisesRegex(ValueError, "Invalid expression for field TCD"):
                load_requirements(str(path))

    def test_bad_screenshot_does_not_stop_pipeline_or_suppress_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workbook_path = base / "requirements.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["RAFIT", "Function", "Auth Object 1", "Auth Object 1 Fields"])
            sheet.append(["X", "Test", "S_TCODE", "TCD = SU01"])
            workbook.save(workbook_path)
            output_dir = base / "output"
            with patch("core.pipeline.read_screenshot",
                       side_effect=ValueError("unreadable image")):
                result = run_job(
                    [("broken.png", b"not-an-image")], str(workbook_path),
                    str(output_dir), True, lambda *_: None,
                )
            self.assertEqual(result["results"][0]["status"], "FAIL")
            self.assertEqual(result["summary"]["criteria_matched"], 0)
            self.assertEqual(result["summary"]["criteria_total"], 1)
            self.assertEqual(result["summary"]["criteria_match_percent"], 0.0)
            self.assertIn("unreadable image", result["screens"][0]["error"])
            self.assertIsNone(result["summary"]["ocr_confidence_percent"])
            self.assertTrue((output_dir / "SAP_Entry_Check_Report.xlsx").is_file())


if __name__ == "__main__":
    unittest.main()
