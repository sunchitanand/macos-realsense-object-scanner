from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import open3d as o3d

from object_scanner.reconstruction import ReconstructionError, depth_units_per_metre, write_printable_stl_mm


class StlExportTests(unittest.TestCase):
    def test_sensor_depth_units_are_converted_for_open3d(self) -> None:
        self.assertEqual(depth_units_per_metre(0.001), 1000.0)
        self.assertEqual(depth_units_per_metre(0.00025), 4000.0)
        with self.assertRaises(ReconstructionError):
            depth_units_per_metre(0.0)

    def test_export_scales_metres_to_millimetres(self) -> None:
        mesh = o3d.geometry.TriangleMesh.create_box(width=0.12, height=0.08, depth=0.04)
        mesh.compute_vertex_normals()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.stl"
            report = write_printable_stl_mm(o3d, mesh, path)
            loaded = o3d.io.read_triangle_mesh(str(path))
            extents = np.sort(loaded.get_axis_aligned_bounding_box().get_extent())

        np.testing.assert_allclose(extents, [40.0, 80.0, 120.0], atol=0.05)
        self.assertEqual(report["units"], "millimetres")
        self.assertEqual(report["label"], "READY")


if __name__ == "__main__":
    unittest.main()
