from __future__ import annotations

import unittest

from object_scanner.measurement import dimensions_from_extents, quality_summary, slugify_scan_name


class SlugifyScanNameTests(unittest.TestCase):
    def test_normalizes_human_name(self) -> None:
        self.assertEqual(slugify_scan_name("  Red Mug #2  "), "red-mug-2")

    def test_rejects_name_without_letters_or_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "letter or number"):
            slugify_scan_name(" --- ")


class DimensionTests(unittest.TestCase):
    def test_sorts_and_converts_metres_to_millimetres(self) -> None:
        self.assertEqual(
            dimensions_from_extents([0.0412, 0.1234, 0.0789]),
            {"length_mm": 123.4, "width_mm": 78.9, "height_mm": 41.2},
        )

    def test_rejects_non_positive_extent(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive extents"):
            dimensions_from_extents([0.1, 0.0, 0.2])


class QualitySummaryTests(unittest.TestCase):
    def test_reports_good_scan_with_text_and_structural_signal(self) -> None:
        summary = quality_summary(90, 100, 20_000)
        self.assertEqual(summary["label"], "GOOD")
        self.assertEqual(summary["border"], "solid")

    def test_reports_weak_scan(self) -> None:
        summary = quality_summary(8, 100, 400)
        self.assertEqual(summary["label"], "WEAK")
        self.assertEqual(summary["border"], "dotted")


if __name__ == "__main__":
    unittest.main()
