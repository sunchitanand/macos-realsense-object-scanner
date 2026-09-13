from __future__ import annotations

import unittest

import numpy as np
import open3d as o3d

from object_scanner.reconstruction import ReconstructionError, _extract_object_points


class ObjectExtractionTests(unittest.TestCase):
    def test_extracts_compact_object_supported_by_larger_plane(self) -> None:
        grid = np.linspace(-0.28, 0.28, 57)
        plane_x, plane_y = np.meshgrid(grid, grid)
        plane_points = np.column_stack(
            (plane_x.ravel(), plane_y.ravel(), np.zeros(plane_x.size))
        )

        rng = np.random.default_rng(7)
        object_points = np.column_stack(
            (
                rng.uniform(-0.05, 0.05, 2400),
                rng.uniform(-0.04, 0.04, 2400),
                rng.uniform(0.004, 0.09, 2400),
            )
        )
        cloud = o3d.geometry.PointCloud()
        cloud.points = o3d.utility.Vector3dVector(np.vstack((plane_points, object_points)))

        extracted, report = _extract_object_points(
            o3d,
            np,
            cloud,
            voxel_length=0.005,
            max_object_size_m=0.5,
        )

        self.assertTrue(report["support_plane_removed"])
        self.assertGreater(len(extracted.points), 1500)
        self.assertLess(float(np.asarray(extracted.points)[:, 2].max()), 0.11)

    def test_rejects_scene_without_compact_object_candidate(self) -> None:
        points = np.column_stack(
            (
                np.linspace(-1.0, 1.0, 400),
                np.zeros(400),
                np.zeros(400),
            )
        )
        cloud = o3d.geometry.PointCloud()
        cloud.points = o3d.utility.Vector3dVector(points)

        with self.assertRaisesRegex(ReconstructionError, "supported by a larger flat plane"):
            _extract_object_points(
                o3d,
                np,
                cloud,
                voxel_length=0.005,
                max_object_size_m=0.5,
            )


if __name__ == "__main__":
    unittest.main()
