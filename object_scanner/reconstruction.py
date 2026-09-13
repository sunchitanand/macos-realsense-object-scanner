"""Offline RGB-D odometry, TSDF fusion, object extraction, and measurement."""

from __future__ import annotations

import copy
import json
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .measurement import dimensions_from_extents, quality_summary

ProgressCallback = Callable[[int, str], None]


class ReconstructionError(RuntimeError):
    """Raised when a scan cannot be reconstructed into a useful model."""


def reconstruct_scan(scan_dir: Path, progress: ProgressCallback | None = None) -> dict[str, Any]:
    """Fuse a captured scan and return metric object dimensions and file paths."""
    try:
        import numpy as np
        import open3d as o3d
    except ImportError as exc:
        raise ReconstructionError(
            "Open3D is not installed. Launch the app through run_object_scanner.sh."
        ) from exc

    scan_dir = Path(scan_dir)
    manifest_path = scan_dir / "manifest.json"
    intrinsic_path = scan_dir / "camera_intrinsic.json"
    if not manifest_path.is_file() or not intrinsic_path.is_file():
        raise ReconstructionError("The scan is missing its manifest or camera intrinsics.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    color_paths = sorted((scan_dir / "color").glob("*.jpg"))
    depth_paths = sorted((scan_dir / "depth").glob("*.png"))
    if len(color_paths) != len(depth_paths):
        raise ReconstructionError("Color and depth frame counts do not match.")
    if len(color_paths) < 15:
        raise ReconstructionError("Capture at least 15 RGB-D frames before reconstruction.")

    intrinsic_data = json.loads(intrinsic_path.read_text(encoding="utf-8"))
    matrix = intrinsic_data["intrinsic_matrix"]
    intrinsic = o3d.camera.PinholeCameraIntrinsic(
        int(intrinsic_data["width"]),
        int(intrinsic_data["height"]),
        float(matrix[0]),
        float(matrix[4]),
        float(matrix[6]),
        float(matrix[7]),
    )

    capture_config = manifest.get("capture", {})
    depth_scale_m = float(capture_config.get("depth_scale_m_per_unit", 0.001))
    depth_scale = depth_units_per_metre(depth_scale_m)
    depth_trunc = float(capture_config.get("max_depth_m", 1.5))
    voxel_length = max(0.0025, min(0.008, depth_trunc / 500.0))
    sdf_trunc = voxel_length * 5.0

    odometry_option = o3d.pipelines.odometry.OdometryOption()
    odometry_option.depth_max = depth_trunc
    pose_graph = o3d.pipelines.registration.PoseGraph()
    pose_graph.nodes.append(o3d.pipelines.registration.PoseGraphNode(np.eye(4)))
    world_to_camera = np.eye(4)
    accepted_indices = [0]
    rejected_frames = 0

    report = progress or (lambda _percent, _message: None)
    report(2, "Loading the first aligned RGB-D frame")
    source_rgbd = _load_rgbd(o3d, color_paths[0], depth_paths[0], depth_scale, depth_trunc)

    report(5, "Tracking the camera around the object")
    total_pairs = len(color_paths) - 1
    for index in range(1, len(color_paths)):
        target_rgbd = _load_rgbd(o3d, color_paths[index], depth_paths[index], depth_scale, depth_trunc)
        success, source_to_target, information = o3d.pipelines.odometry.compute_rgbd_odometry(
            source_rgbd,
            target_rgbd,
            intrinsic,
            np.eye(4),
            o3d.pipelines.odometry.RGBDOdometryJacobianFromHybridTerm(),
            odometry_option,
        )

        if success and _plausible_motion(np, source_to_target):
            source_node_id = len(accepted_indices) - 1
            target_node_id = source_node_id + 1
            world_to_camera = source_to_target @ world_to_camera
            camera_to_world = np.linalg.inv(world_to_camera)
            pose_graph.nodes.append(o3d.pipelines.registration.PoseGraphNode(camera_to_world))
            pose_graph.edges.append(
                o3d.pipelines.registration.PoseGraphEdge(
                    source_node_id,
                    target_node_id,
                    source_to_target,
                    information,
                    False,
                    1.0,
                )
            )
            source_rgbd = target_rgbd
            accepted_indices.append(index)
        else:
            rejected_frames += 1

        percent = 5 + int(38 * index / max(1, total_pairs))
        report(percent, f"Tracking frame {index + 1}/{len(color_paths)}")

    accepted_frames = len(accepted_indices)
    if accepted_frames < 10 or accepted_frames / len(color_paths) < 0.25:
        raise ReconstructionError(
            "Camera tracking was lost too often. Move more slowly, keep the object in view, "
            "and add textured background features around it."
        )

    report(45, "Finding loop closures around the camera orbit")
    loop_closure_edges = 0
    poses = [np.asarray(node.pose) for node in pose_graph.nodes]
    candidates = _loop_closure_candidates(np, poses)
    for candidate_index, (source_node_id, target_node_id) in enumerate(candidates):
        source_index = accepted_indices[source_node_id]
        target_index = accepted_indices[target_node_id]
        source_loop = _load_rgbd(
            o3d,
            color_paths[source_index],
            depth_paths[source_index],
            depth_scale,
            depth_trunc,
        )
        target_loop = _load_rgbd(
            o3d,
            color_paths[target_index],
            depth_paths[target_index],
            depth_scale,
            depth_trunc,
        )
        initial = np.linalg.inv(poses[target_node_id]) @ poses[source_node_id]
        success, source_to_target, information = o3d.pipelines.odometry.compute_rgbd_odometry(
            source_loop,
            target_loop,
            intrinsic,
            initial,
            o3d.pipelines.odometry.RGBDOdometryJacobianFromHybridTerm(),
            odometry_option,
        )
        if success and _transform_near_initial(np, source_to_target, initial):
            pose_graph.edges.append(
                o3d.pipelines.registration.PoseGraphEdge(
                    source_node_id,
                    target_node_id,
                    source_to_target,
                    information,
                    True,
                    1.0,
                )
            )
            loop_closure_edges += 1
        report(
            45 + int(8 * (candidate_index + 1) / max(1, len(candidates))),
            f"Checking loop closure {candidate_index + 1}/{len(candidates)}",
        )

    report(54, "Optimizing the complete camera trajectory")
    o3d.pipelines.registration.global_optimization(
        pose_graph,
        o3d.pipelines.registration.GlobalOptimizationLevenbergMarquardt(),
        o3d.pipelines.registration.GlobalOptimizationConvergenceCriteria(),
        o3d.pipelines.registration.GlobalOptimizationOption(
            max_correspondence_distance=max(0.03, voxel_length * 10.0),
            edge_prune_threshold=0.25,
            preference_loop_closure=0.1,
            reference_node=0,
        ),
    )

    report(57, "Fusing optimized RGB-D poses")
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel_length,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )
    trajectory: list[list[list[float]]] = []
    for node_id, frame_index in enumerate(accepted_indices):
        rgbd = _load_rgbd(
            o3d,
            color_paths[frame_index],
            depth_paths[frame_index],
            depth_scale,
            depth_trunc,
        )
        camera_to_world = np.asarray(pose_graph.nodes[node_id].pose)
        volume.integrate(rgbd, intrinsic, np.linalg.inv(camera_to_world))
        trajectory.append(camera_to_world.tolist())
        report(
            57 + int(10 * (node_id + 1) / accepted_frames),
            f"Fusing accepted frame {node_id + 1}/{accepted_frames}",
        )

    report(68, "Extracting the fused surface")
    raw_mesh = volume.extract_triangle_mesh()
    raw_mesh.compute_vertex_normals()
    _clean_mesh(raw_mesh)
    if len(raw_mesh.vertices) < 500:
        raise ReconstructionError("The fused mesh is too small to measure.")

    raw_mesh_path = scan_dir / "model_raw.ply"
    o3d.io.write_triangle_mesh(
        str(raw_mesh_path),
        raw_mesh,
        write_ascii=False,
        compressed=False,
        write_vertex_normals=True,
        write_vertex_colors=True,
    )

    report(73, "Removing the support plane and finding the object")
    scene_points = volume.extract_point_cloud().voxel_down_sample(voxel_length)
    scene_points, _ = scene_points.remove_statistical_outlier(nb_neighbors=24, std_ratio=2.0)
    object_points, extraction = _extract_object_points(
        o3d,
        np,
        scene_points,
        voxel_length=voxel_length,
        max_object_size_m=min(1.2, depth_trunc),
    )
    if len(object_points.points) < 500:
        raise ReconstructionError(
            "Could not isolate the object. Scan it on a clear flat surface with the background farther away."
        )

    report(80, "Calculating minimum-volume dimensions")
    object_box = object_points.get_minimal_oriented_bounding_box(robust=True)
    dimensions = dimensions_from_extents(object_box.extent)
    object_points_path = scan_dir / "object_points.ply"
    o3d.io.write_point_cloud(str(object_points_path), object_points, write_ascii=False, compressed=False)

    box_lines = o3d.geometry.LineSet.create_from_oriented_bounding_box(object_box)
    box_lines.paint_uniform_color([0.95, 0.95, 0.95])
    o3d.io.write_line_set(str(scan_dir / "object_bounds.ply"), box_lines, write_ascii=True)

    report(86, "Building an object-only surface mesh")
    object_mesh_path: Path | None = None
    printable_stl_path: Path | None = None
    print_readiness: dict[str, Any] | None = None
    mesh_warning: str | None = None
    try:
        object_mesh, printable_mesh = _create_object_mesh(o3d, np, object_points, object_box, voxel_length)
        object_mesh_path = scan_dir / "object_mesh.ply"
        o3d.io.write_triangle_mesh(
            str(object_mesh_path),
            object_mesh,
            write_ascii=False,
            compressed=False,
            write_vertex_normals=True,
            write_vertex_colors=True,
        )
        printable_stl_path = scan_dir / "object_printable.stl"
        print_readiness = write_printable_stl_mm(o3d, printable_mesh, printable_stl_path)
    except Exception as exc:
        mesh_warning = f"Object point cloud succeeded, but printable meshing failed: {exc}"

    report(94, "Writing the interactive preview")
    preview_path = scan_dir / "preview_points.json"
    _write_preview(np, object_points, preview_path)
    (scan_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "camera_to_world": trajectory,
                "frame_indices": accepted_indices,
                "loop_closure_edges": loop_closure_edges,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    quality = quality_summary(accepted_frames, len(color_paths), len(object_points.points))
    result = {
        "scan_id": manifest["scan_id"],
        "status": "complete",
        "completed_at": datetime.now(tz=UTC).isoformat(),
        "dimensions": dimensions,
        "dimension_basis": "minimum-volume oriented bounding box",
        "quality": quality,
        "tracking": {
            "accepted_frames": accepted_frames,
            "rejected_frames": rejected_frames,
            "total_frames": len(color_paths),
            "loop_closure_edges": loop_closure_edges,
        },
        "extraction": extraction,
        "voxel_length_mm": round(voxel_length * 1000.0, 2),
        "files": {
            "raw_mesh": raw_mesh_path.name,
            "object_points": object_points_path.name,
            "object_bounds": "object_bounds.ply",
            "object_mesh": object_mesh_path.name if object_mesh_path else None,
            "printable_stl": printable_stl_path.name if printable_stl_path else None,
            "preview": preview_path.name,
            "trajectory": "trajectory.json",
        },
        "print_readiness": print_readiness,
        "warnings": [warning for warning in [mesh_warning] if warning],
    }
    (scan_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    manifest["status"] = "complete"
    manifest["result"] = "result.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _restore_invoking_user_ownership(scan_dir)
    report(100, "Object model and dimensions are ready")
    return result


def _load_rgbd(o3d: Any, color_path: Path, depth_path: Path, depth_scale: float, depth_trunc: float) -> Any:
    color = o3d.io.read_image(str(color_path))
    depth = o3d.io.read_image(str(depth_path))
    return o3d.geometry.RGBDImage.create_from_color_and_depth(
        color,
        depth,
        depth_scale=depth_scale,
        depth_trunc=depth_trunc,
        convert_rgb_to_intensity=False,
    )


def depth_units_per_metre(depth_scale_m_per_unit: float) -> float:
    """Convert a sensor's metric depth unit into Open3D's units-per-metre scale."""
    if not math.isfinite(depth_scale_m_per_unit) or depth_scale_m_per_unit <= 0:
        raise ReconstructionError("The captured depth scale must be a positive finite value.")
    return 1.0 / depth_scale_m_per_unit


def _plausible_motion(np: Any, transform: Any) -> bool:
    if not np.isfinite(transform).all():
        return False
    translation_m = float(np.linalg.norm(transform[:3, 3]))
    rotation_trace = float(np.trace(transform[:3, :3]))
    cosine = max(-1.0, min(1.0, (rotation_trace - 1.0) / 2.0))
    rotation_deg = math.degrees(math.acos(cosine))
    return translation_m <= 0.25 and rotation_deg <= 35.0


def _loop_closure_candidates(np: Any, poses: list[Any], max_candidates: int = 24) -> list[tuple[int, int]]:
    """Choose spatially nearby, non-adjacent poses for loop-closure verification."""
    if len(poses) < 12:
        return []
    minimum_gap = max(10, len(poses) // 8)
    ranked: list[tuple[float, int, int]] = []
    for target_node_id in range(minimum_gap, len(poses)):
        target_position = poses[target_node_id][:3, 3]
        for source_node_id in range(0, target_node_id - minimum_gap + 1):
            distance = float(np.linalg.norm(target_position - poses[source_node_id][:3, 3]))
            if distance <= 0.25:
                ranked.append((distance, source_node_id, target_node_id))

    ranked.sort(key=lambda item: item[0])
    selected: list[tuple[int, int]] = []
    used_targets: set[int] = set()
    for _distance, source_node_id, target_node_id in ranked:
        if target_node_id in used_targets:
            continue
        selected.append((source_node_id, target_node_id))
        used_targets.add(target_node_id)
        if len(selected) >= max_candidates:
            break
    return selected


def _transform_near_initial(np: Any, estimated: Any, initial: Any) -> bool:
    if not np.isfinite(estimated).all():
        return False
    correction = estimated @ np.linalg.inv(initial)
    translation_m = float(np.linalg.norm(correction[:3, 3]))
    rotation_trace = float(np.trace(correction[:3, :3]))
    cosine = max(-1.0, min(1.0, (rotation_trace - 1.0) / 2.0))
    rotation_deg = math.degrees(math.acos(cosine))
    return translation_m <= 0.08 and rotation_deg <= 12.0


def _clean_mesh(mesh: Any) -> None:
    mesh.remove_duplicated_vertices()
    mesh.remove_duplicated_triangles()
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()


def _extract_object_points(
    o3d: Any,
    np: Any,
    points: Any,
    *,
    voxel_length: float,
    max_object_size_m: float,
) -> tuple[Any, dict[str, Any]]:
    extraction: dict[str, Any] = {"support_plane_removed": False, "cluster_count": 0}
    plane_model: Any | None = None
    plane_extent: Any | None = None
    without_plane: Any | None = None
    if len(points.points) >= 500:
        try:
            plane, inliers = points.segment_plane(
                distance_threshold=max(0.005, voxel_length * 2.0),
                ransac_n=3,
                num_iterations=1200,
            )
            plane_points = points.select_by_index(inliers)
            plane_extent = np.asarray(plane_points.get_axis_aligned_bounding_box().get_extent())
            plane_is_broad = float(np.sort(plane_extent)[-2]) >= 0.18
            enough_support = len(inliers) >= max(150, int(len(points.points) * 0.08))
            if enough_support and plane_is_broad:
                plane_model = np.asarray(plane, dtype=float)
                without_plane = points.select_by_index(inliers, invert=True)
                extraction["support_plane"] = [round(float(value), 6) for value in plane]
                extraction["support_plane_points"] = len(inliers)
        except RuntimeError:
            pass

    candidates: list[tuple[int, float, Any]] = []
    cluster_count = 0
    if without_plane is not None and plane_model is not None and plane_extent is not None:
        supported, supported_count = _object_cluster_candidates(
            np,
            without_plane,
            voxel_length=voxel_length,
            max_object_size_m=max_object_size_m,
            support_plane=plane_model,
            support_plane_extent=plane_extent,
        )
        cluster_count += supported_count
        if supported:
            candidates = supported
            extraction["support_plane_removed"] = True

    if not candidates:
        raise ReconstructionError(
            "Could not identify an object supported by a larger flat plane. Place the object on "
            "a clear tabletop, keep the table visible around every side, and keep hands out of "
            "the capture."
        )

    extraction["cluster_count"] = cluster_count
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen = candidates[0][2]

    extraction["object_points_before_meshing"] = len(chosen.points)
    return chosen, extraction


def _object_cluster_candidates(
    np: Any,
    points: Any,
    *,
    voxel_length: float,
    max_object_size_m: float,
    support_plane: Any | None = None,
    support_plane_extent: Any | None = None,
) -> tuple[list[tuple[int, float, Any]], int]:
    labels = np.asarray(
        points.cluster_dbscan(
            eps=max(0.012, voxel_length * 4.0),
            min_points=30,
            print_progress=False,
        )
    )
    cluster_ids = [int(label) for label in np.unique(labels) if label >= 0]
    candidates: list[tuple[int, float, Any]] = []
    for cluster_id in cluster_ids:
        indices = np.flatnonzero(labels == cluster_id).tolist()
        cluster = points.select_by_index(indices)
        point_count = len(cluster.points)
        bounds = cluster.get_axis_aligned_bounding_box()
        extent = np.asarray(bounds.get_extent())
        sorted_extent = np.sort(extent)
        if point_count < 100 or float(extent.max()) > max_object_size_m * 1.25:
            continue
        if float(sorted_extent[-2]) < max(0.015, voxel_length * 3.0):
            continue
        if support_plane is not None and support_plane_extent is not None:
            if not _cluster_is_supported_by_plane(
                np,
                cluster,
                support_plane,
                support_plane_extent,
                voxel_length,
            ):
                continue
        distance = float(np.linalg.norm(bounds.get_center()))
        candidates.append((point_count, distance, cluster))
    return candidates, len(cluster_ids)


def _cluster_is_supported_by_plane(
    np: Any,
    cluster: Any,
    plane: Any,
    plane_extent: Any,
    voxel_length: float,
) -> bool:
    points = np.asarray(cluster.points)
    normal = np.asarray(plane[:3], dtype=float)
    normal_length = float(np.linalg.norm(normal))
    if normal_length <= 1e-9:
        return False
    distances = (points @ normal + float(plane[3])) / normal_length
    direction = 1.0 if float(np.median(distances)) >= 0 else -1.0
    oriented = distances * direction
    tolerance = max(0.008, voxel_length * 2.0)
    mostly_one_side = float(np.mean(oriented >= -tolerance)) >= 0.9
    near_plane = float(np.quantile(np.abs(distances), 0.1)) <= max(0.025, voxel_length * 6.0)

    object_extent = np.sort(np.asarray(cluster.get_axis_aligned_bounding_box().get_extent()))[-2:]
    support_extent = np.sort(np.asarray(plane_extent))[-2:]
    plane_is_larger = bool(np.all(support_extent >= object_extent * 1.25))
    return mostly_one_side and near_plane and plane_is_larger


def _create_object_mesh(
    o3d: Any,
    np: Any,
    points: Any,
    object_box: Any,
    voxel_length: float,
) -> tuple[Any, Any]:
    mesh_points = points.voxel_down_sample(max(0.0025, voxel_length * 0.8))
    mesh_points.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(
            radius=max(0.015, voxel_length * 6.0),
            max_nn=40,
        )
    )
    mesh_points.orient_normals_consistent_tangent_plane(min(30, max(10, len(mesh_points.points) // 100)))
    poisson_mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        mesh_points,
        depth=8,
        scale=1.08,
        linear_fit=False,
    )
    poisson_mesh.compute_vertex_normals()
    _clean_mesh(poisson_mesh)
    mesh = copy.deepcopy(poisson_mesh)
    densities_array = np.asarray(densities)
    if len(densities_array):
        cutoff = float(np.quantile(densities_array, 0.03))
        mesh.remove_vertices_by_mask(densities_array < cutoff)

    crop_box = copy.deepcopy(object_box)
    crop_box.scale(1.08, crop_box.center)
    mesh = mesh.crop(crop_box)
    mesh.compute_vertex_normals()
    _clean_mesh(mesh)
    printable_mesh = _largest_mesh_component(o3d, np, mesh)
    if len(mesh.vertices) < 100:
        raise ReconstructionError("Poisson meshing returned too little geometry.")
    if len(printable_mesh.vertices) < 100:
        raise ReconstructionError("Printable Poisson surface returned too little geometry.")
    return mesh, printable_mesh


def _largest_mesh_component(o3d: Any, np: Any, mesh: Any) -> Any:
    """Keep the largest closed Poisson component without cutting its surface."""
    if len(mesh.triangles) == 0:
        return mesh
    triangle_clusters, cluster_counts, _cluster_areas = mesh.cluster_connected_triangles()
    cluster_counts_array = np.asarray(cluster_counts)
    if not len(cluster_counts_array):
        return mesh
    largest_cluster = int(cluster_counts_array.argmax())
    remove_mask = np.asarray(triangle_clusters) != largest_cluster
    cleaned = copy.deepcopy(mesh)
    cleaned.remove_triangles_by_mask(remove_mask)
    cleaned.remove_unreferenced_vertices()
    cleaned.compute_vertex_normals()
    _clean_mesh(cleaned)
    return cleaned


def write_printable_stl_mm(o3d: Any, mesh_m: Any, path: Path) -> dict[str, Any]:
    """Write a metre-scale Open3D mesh as millimetre-scale STL geometry."""
    stl_mesh = copy.deepcopy(mesh_m)
    stl_mesh.scale(1000.0, center=[0.0, 0.0, 0.0])
    stl_mesh.compute_triangle_normals()
    stl_mesh.compute_vertex_normals()
    _clean_mesh(stl_mesh)
    success = o3d.io.write_triangle_mesh(
        str(path),
        stl_mesh,
        write_ascii=False,
        compressed=False,
        write_vertex_normals=True,
        write_vertex_colors=False,
    )
    if not success:
        raise ReconstructionError(f"Open3D could not write STL output to {path}.")

    watertight = bool(stl_mesh.is_watertight())
    edge_manifold = bool(stl_mesh.is_edge_manifold(allow_boundary_edges=False))
    vertex_manifold = bool(stl_mesh.is_vertex_manifold())
    return {
        "label": "READY" if watertight and edge_manifold and vertex_manifold else "CHECK",
        "units": "millimetres",
        "watertight": watertight,
        "edge_manifold": edge_manifold,
        "vertex_manifold": vertex_manifold,
        "triangles": len(stl_mesh.triangles),
    }


def _write_preview(np: Any, points: Any, path: Path, max_points: int = 25_000) -> None:
    positions = np.asarray(points.points)
    colors = np.asarray(points.colors)
    if len(positions) > max_points:
        selected = np.linspace(0, len(positions) - 1, max_points, dtype=int)
        positions = positions[selected]
        colors = colors[selected] if len(colors) else colors
    if not len(colors):
        colors = np.full_like(positions, 0.78)

    payload = {
        "points": np.round(positions, 5).tolist(),
        "colors": np.round(colors, 3).tolist(),
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _restore_invoking_user_ownership(root: Path) -> None:
    user_id = os.environ.get("SUDO_UID")
    group_id = os.environ.get("SUDO_GID")
    if not user_id or not group_id:
        return
    uid = int(user_id)
    gid = int(group_id)
    for directory, subdirectories, files in os.walk(root):
        os.chown(directory, uid, gid)
        for name in subdirectories:
            os.chown(Path(directory) / name, uid, gid)
        for name in files:
            os.chown(Path(directory) / name, uid, gid)
