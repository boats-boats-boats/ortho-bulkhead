#!/usr/bin/env python3
"""
Cabin Window CNC Profile Generator
----------------------------------
Processes photographs of paper window templates with 12.0" calibration crosshairs
into 1:1 production-ready DXF (inches) and visual verification SVG/PNG overlays.

Features:
1. Perspective Rectification (Planar Homography):
   - Auto-detection of '+' crosshairs or interactive OpenCV point selection,
     plus CLI manual coordinate override for headless/automated runs.
   - Sub-pixel corner/crosshair refinement.
   - Homography matrix computation (`cv2.findHomography` / `cv2.getPerspectiveTransform`)
     warping onto an orthographic plane at a calibrated PPI (default 200 PPI).
2. Contour Extraction & Cleaning:
   - Adaptive & Otsu thresholding with morphological cleanup to isolate the fine-point perimeter line.
   - Centerline / primary outer boundary contour extraction.
   - Douglas-Peucker polygon approximation (`cv2.approxPolyDP`) and periodic B-spline
     smoothing (`scipy.interpolate.splprep` / `splev`) to preserve fair marine cabin-top curves.
3. DXF & SVG Generation:
   - Scales output coordinates to real-world inches (1.0 unit = 1.0 inch, $INSUNITS = 1).
   - Exports closed `LWPOLYLINE` and `SPLINE` entities via `ezdxf`.
   - Exports a 1:1 SVG profile and a visual verification SVG/PNG overlay showing the
     extracted vector path over the rectified template image.
4. Dimensional Verification Readout:
   - Computes and prints overall length (width), max height, bounding box diagonals,
     perimeter length, and enclosed area for physical comparison against the original window.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import ezdxf
import numpy as np
from scipy.interpolate import splprep, splev


@dataclass
class VerificationReport:
    image_path: str
    ppi: float
    grid_width_in: float
    grid_height_in: float
    overall_length_in: float
    max_height_in: float
    bbox_diagonal_in: float
    min_area_length_in: float
    min_area_height_in: float
    min_area_diagonal_in: float
    perimeter_in: float
    area_sq_in: float
    reprojection_error_px: float
    dxf_path: str
    svg_overlay_path: str


def parse_points_arg(points_str: str) -> np.ndarray:
    """
    Parse a string of coordinates like 'x1,y1 x2,y2 x3,y3 x4,y4' or JSON list
    into an (N, 2) float32 numpy array.
    """
    points_str = points_str.strip()
    if points_str.startswith("["):
        pts = np.array(json.loads(points_str), dtype=np.float32)
        return pts
    pairs = points_str.replace(";", " ").split()
    pts = []
    for pair in pairs:
        x_str, y_str = pair.split(",")
        pts.append([float(x_str), float(y_str)])
    return np.array(pts, dtype=np.float32)


def refine_crosshair_center(gray: np.ndarray, pt: Tuple[float, float], search_radius: int = 25) -> Tuple[float, float]:
    """
    Refines a rough click or detection near a '+' crosshair to its sub-pixel intersection center.
    Uses a synthesized '+' kernel match followed by quadratic / centroid sub-pixel refinement.
    """
    h, w = gray.shape[:2]
    cx, cy = int(round(pt[0])), int(round(pt[1]))
    x0 = max(0, cx - search_radius)
    y0 = max(0, cy - search_radius)
    x1 = min(w, cx + search_radius + 1)
    y1 = min(h, cy + search_radius + 1)

    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return pt

    # Invert so dark lines have high intensity
    inv = 255.0 - roi.astype(np.float32)
    inv = np.clip(inv - np.percentile(inv, 50), 0, None)

    # Create a '+' crosshair filter kernel
    ksize = min(21, min(roi.shape[0], roi.shape[1]) - 2)
    if ksize < 5:
        return pt
    if ksize % 2 == 0:
        ksize -= 1
    kernel = np.zeros((ksize, ksize), dtype=np.float32)
    mid = ksize // 2
    kernel[mid, :] = 1.0
    kernel[:, mid] = 1.0
    kernel = cv2.GaussianBlur(kernel, (3, 3), 0.8)

    response = cv2.filter2D(inv, cv2.CV_32F, kernel)
    _, _, _, max_loc = cv2.minMaxLoc(response)
    px, py = max_loc

    # Sub-pixel centroid in a 5x5 window around the peak
    sub_r = 2
    sx0 = max(0, px - sub_r)
    sy0 = max(0, py - sub_r)
    sx1 = min(response.shape[1], px + sub_r + 1)
    sy1 = min(response.shape[0], py + sub_r + 1)
    patch = response[sy0:sy1, sx0:sx1]
    patch = patch - patch.min()
    total = patch.sum()
    if total > 1e-6:
        yy, xx = np.mgrid[sy0:sy1, sx0:sx1]
        sub_x = float((xx * patch).sum() / total)
        sub_y = float((yy * patch).sum() / total)
    else:
        sub_x, sub_y = float(px), float(py)

    return (x0 + sub_x, y0 + sub_y)


def auto_detect_crosshairs(gray: np.ndarray, expected_count: int = 4, ksize: int = 31) -> np.ndarray:
    """
    Attempts automated detection of '+' calibration crosshairs via morphological hit-or-miss /
    matched filtering for horizontal + vertical line intersections.
    Returns ordered points [TL, TR, BR, BL] if 4 points are requested.
    """
    inv = 255 - gray
    # Adaptive threshold to capture fine lines
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 10
    )

    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, 1))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, ksize))

    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horiz_kernel)
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vert_kernel)

    # Crosshair intersections have both horizontal and vertical segments
    joints = cv2.bitwise_and(horiz, vert)
    joints = cv2.dilate(joints, np.ones((5, 5), np.uint8), iterations=1)

    num_labels, _, stats, centroids = cv2.connectedComponentsWithStats(joints)
    if num_labels - 1 < expected_count:
        raise RuntimeError(
            f"Auto-detection found {num_labels - 1} crosshair candidates, needed at least {expected_count}. "
            "Use --interactive or pass --src-points explicitly."
        )

    # Score candidates by cross-correlation with an ideal '+' kernel
    plus_kernel = np.zeros((ksize, ksize), dtype=np.float32)
    mid = ksize // 2
    plus_kernel[mid - 1 : mid + 2, :] = 1.0
    plus_kernel[:, mid - 1 : mid + 2] = 1.0
    response = cv2.filter2D(inv.astype(np.float32), cv2.CV_32F, plus_kernel)

    candidates = []
    for i in range(1, num_labels):
        cx, cy = centroids[i]
        ix, iy = int(round(cx)), int(round(cy))
        score = float(response[iy, ix])
        candidates.append((score, (float(cx), float(cy))))

    candidates.sort(key=lambda item: item[0], reverse=True)
    top_pts = [refine_crosshair_center(gray, pt) for _, pt in candidates[:expected_count]]
    pts_arr = np.array(top_pts, dtype=np.float32)
    if expected_count == 4:
        return order_quad_points(pts_arr)
    return pts_arr


def order_quad_points(pts: np.ndarray) -> np.ndarray:
    """
    Orders 4 points clockwise starting from top-left: [TL, TR, BR, BL].
    """
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # Top-left
    rect[2] = pts[np.argmax(s)]  # Bottom-right

    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # Top-right
    rect[3] = pts[np.argmax(diff)]  # Bottom-left
    return rect


def pick_crosshairs_interactive(image: np.ndarray, gray: np.ndarray, num_points: int = 4) -> np.ndarray:
    """
    Interactive OpenCV GUI window to click the calibration crosshairs in order:
    Top-Left, Top-Right, Bottom-Right, Bottom-Left (or N points matching --dst-points).
    Automatically refines each click to the nearest '+' intersection center.
    """
    clicked: List[Tuple[float, float]] = []
    display = image.copy()
    win_name = "Pick Calibration Crosshairs (Click TL -> TR -> BR -> BL, 'r' reset, 'q'/Enter confirm)"

    def on_mouse(event, x, y, flags, param):
        nonlocal display
        if event == cv2.EVENT_LBUTTONDOWN and len(clicked) < num_points:
            refined = refine_crosshair_center(gray, (float(x), float(y)))
            clicked.append(refined)
            display = image.copy()
            for idx, (rx, ry) in enumerate(clicked):
                cv2.drawMarker(
                    display,
                    (int(round(rx)), int(round(ry))),
                    (0, 0, 255),
                    markerType=cv2.MARKER_CROSS,
                    markerSize=24,
                    thickness=2,
                )
                cv2.putText(
                    display,
                    f"#{idx + 1} ({rx:.1f},{ry:.1f})",
                    (int(round(rx)) + 10, int(round(ry)) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 255, 0),
                    2,
                )
            cv2.imshow(win_name, display)

    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.imshow(win_name, display)
    cv2.setMouseCallback(win_name, on_mouse)

    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10, ord("q")) and len(clicked) == num_points:
            break
        if key == ord("r"):
            clicked.clear()
            display = image.copy()
            cv2.imshow(win_name, display)
        if key == 27:  # ESC
            cv2.destroyAllWindows()
            raise RuntimeError("Interactive selection cancelled by user.")

    cv2.destroyAllWindows()
    pts = np.array(clicked, dtype=np.float32)
    return order_quad_points(pts) if num_points == 4 else pts


def rectify_image(
    image: np.ndarray,
    src_pts: np.ndarray,
    dst_pts_in: np.ndarray,
    ppi: float = 200.0,
    margin_in: float = 4.0,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """
    Computes the planar homography from `src_pts` (pixels) to `dst_pts_in` (inches)
    and warps `image` onto an orthographic canvas at `ppi` pixels per inch, with
    `margin_in` inches of padding around the calibration grid.

    Returns:
        warped_image, homography_matrix, reprojection_error_px
    """
    dst_pts_px = (dst_pts_in + margin_in) * ppi

    if len(src_pts) == 4 and len(dst_pts_px) == 4:
        H = cv2.getPerspectiveTransform(src_pts.astype(np.float32), dst_pts_px.astype(np.float32))
    else:
        H, _ = cv2.findHomography(src_pts, dst_pts_px, method=cv2.RANSAC, ransacReprojThreshold=3.0)

    if H is None:
        raise RuntimeError("Failed to compute homography matrix from calibration points.")

    # Compute reprojection error in destination pixels
    projected = cv2.perspectiveTransform(src_pts.reshape(-1, 1, 2).astype(np.float32), H).reshape(-1, 2)
    reproj_error = float(np.sqrt(np.mean(np.sum((projected - dst_pts_px) ** 2, axis=1))))

    max_x_in = float(dst_pts_in[:, 0].max()) + 2.0 * margin_in
    max_y_in = float(dst_pts_in[:, 1].max()) + 2.0 * margin_in
    out_w = int(round(max_x_in * ppi))
    out_h = int(round(max_y_in * ppi))

    # Ensure the entire warped source image fits if the window extends beyond the grid + margin
    h_img, w_img = image.shape[:2]
    img_corners = np.array([[[0, 0]], [[w_img, 0]], [[w_img, h_img]], [[0, h_img]]], dtype=np.float32)
    warped_corners = cv2.perspectiveTransform(img_corners, H).reshape(-1, 2)
    out_w = max(out_w, int(np.ceil(warped_corners[:, 0].max())))
    out_h = max(out_h, int(np.ceil(warped_corners[:, 1].max())))

    warped = cv2.warpPerspective(
        image,
        H,
        (out_w, out_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return warped, H, reproj_error


def mask_crosshairs_on_rectified(
    binary: np.ndarray,
    dst_pts_in: np.ndarray,
    ppi: float,
    margin_in: float,
    mask_radius_in: float = 0.75,
) -> np.ndarray:
    """
    Clears small regions around the known calibration crosshair positions on the
    rectified binary image so crosshairs are never mistaken for the window contour.
    """
    cleaned = binary.copy()
    r_px = int(round(mask_radius_in * ppi))
    for pt_in in dst_pts_in:
        cx = int(round((pt_in[0] + margin_in) * ppi))
        cy = int(round((pt_in[1] + margin_in) * ppi))
        cv2.circle(cleaned, (cx, cy), r_px, 0, thickness=-1)
    return cleaned


def extract_window_contour(
    rectified_bgr: np.ndarray,
    dst_pts_in: np.ndarray,
    ppi: float,
    margin_in: float,
    block_size: int = 51,
    thresh_c: int = 12,
    extract_mode: str = "centerline",
) -> np.ndarray:
    """
    Thresholds the rectified image to isolate the dark traced profile line and extracts
    the primary closed window boundary contour in pixel coordinates.

    extract_mode:
      - 'outer': outer boundary of the traced line (`RETR_EXTERNAL` on the line mask)
      - 'inner': inner boundary of the traced closed loop
      - 'centerline': averages the outer and inner boundaries of the pen stroke (true traced line center)
    """
    gray = cv2.cvtColor(rectified_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    if block_size % 2 == 0:
        block_size += 1

    binary = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        thresh_c,
    )

    # Clear known calibration crosshairs
    binary = mask_crosshairs_on_rectified(binary, dst_pts_in, ppi, margin_in)

    # Morphological closing to bridge tiny paper grain gaps in pencil/marker strokes
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, close_k, iterations=2)

    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not contours or hierarchy is None:
        raise RuntimeError("No contours found in rectified image. Adjust threshold parameters.")

    hierarchy = hierarchy[0]
    # Find the largest external contour by enclosed area (ignoring image border artifacts)
    h_img, w_img = closed.shape[:2]
    max_valid_area = 0.90 * h_img * w_img

    best_outer_idx = -1
    best_area = 0.0
    for idx, cnt in enumerate(contours):
        if hierarchy[idx][3] == -1:  # Top-level (outer) contour
            area = cv2.contourArea(cnt)
            if area > best_area and area < max_valid_area:
                best_area = area
                best_outer_idx = idx

    if best_outer_idx == -1:
        raise RuntimeError("Could not identify a primary window perimeter contour.")

    outer_cnt = contours[best_outer_idx].reshape(-1, 2).astype(np.float64)

    # Check if there is a matching inner hole contour (the interior of the closed pen line loop)
    child_idx = hierarchy[best_outer_idx][2]
    best_inner_cnt = None
    best_inner_area = 0.0
    while child_idx != -1:
        area = cv2.contourArea(contours[child_idx])
        if area > best_inner_area:
            best_inner_area = area
            best_inner_cnt = contours[child_idx].reshape(-1, 2).astype(np.float64)
        child_idx = hierarchy[child_idx][0]

    if extract_mode == "outer" or best_inner_cnt is None or best_inner_area < 0.5 * best_area:
        return outer_cnt
    if extract_mode == "inner":
        return best_inner_cnt

    # Centerline mode: resample outer and inner contours to matching parameterizations and average
    n_samples = max(len(outer_cnt), 1000)
    outer_rs = resample_closed_contour(outer_cnt, n_samples)
    inner_rs = resample_closed_contour(best_inner_cnt, n_samples)
    inner_rs = align_contour_phase(outer_rs, inner_rs)
    return 0.5 * (outer_rs + inner_rs)


def resample_closed_contour(pts: np.ndarray, n_points: int) -> np.ndarray:
    """Resamples a closed 2D polygon uniformly by arc length."""
    closed_pts = np.vstack([pts, pts[0]])
    diffs = np.diff(closed_pts, axis=0)
    seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
    cum_len = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total_len = cum_len[-1]
    if total_len <= 0:
        return pts
    target_s = np.linspace(0.0, total_len, n_points, endpoint=False)
    x_interp = np.interp(target_s, cum_len, closed_pts[:, 0])
    y_interp = np.interp(target_s, cum_len, closed_pts[:, 1])
    return np.column_stack([x_interp, y_interp])


def align_contour_phase(ref: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Aligns winding direction and starting index of `target` contour to `ref` contour."""
    # Ensure consistent winding order (signed area)
    def signed_area(p: np.ndarray) -> float:
        return 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1]))

    if np.sign(signed_area(ref)) != np.sign(signed_area(target)):
        target = target[::-1]

    dists = np.sum((target - ref[0]) ** 2, axis=1)
    shift = int(np.argmin(dists))
    return np.roll(target, -shift, axis=0)


def clean_and_smooth_contour(
    contour_px: np.ndarray,
    ppi: float,
    dp_epsilon_in: float = 0.015,
    spline_smoothing: float = 0.05,
    num_eval_points: int = 720,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Cleans hand-drawn jitter from the raw pixel contour and converts to real-world inches
    (with Y flipped so +Y is up in CAD space, and translated so bounding box min is at (0,0)).

    Returns:
        (dp_points_in, spline_points_in)
    """
    # Convert to inches in image space first
    pts_in = contour_px / ppi
    # Flip Y for standard CAD right-handed coordinates (+X right, +Y up)
    pts_in[:, 1] = -pts_in[:, 1]

    # 1. Douglas-Peucker polygon approximation
    cnt_f32 = pts_in.reshape(-1, 1, 2).astype(np.float32)
    dp_approx = cv2.approxPolyDP(cnt_f32, epsilon=dp_epsilon_in, closed=True).reshape(-1, 2).astype(np.float64)

    # 2. Periodic B-spline fairing for smooth marine cabin-top curves
    # Resample raw contour to uniform arc-length points to stabilize B-spline fit
    uniform_pts = resample_closed_contour(pts_in, max(400, min(1200, len(pts_in))))
    closed_input = np.vstack([uniform_pts, uniform_pts[0]])

    try:
        tck, _ = splprep([closed_input[:, 0], closed_input[:, 1]], s=spline_smoothing, per=True, k=3)
        u_fine = np.linspace(0.0, 1.0, num_eval_points, endpoint=False)
        x_s, y_s = splev(u_fine, tck)
        spline_pts = np.column_stack([x_s, y_s])
    except Exception:
        # Fallback to DP points if spline fit fails
        spline_pts = dp_approx.copy()

    # Translate both so the smoothed spline bounding box minimum sits at (0.0, 0.0) inches
    min_xy = spline_pts.min(axis=0)
    spline_pts = spline_pts - min_xy
    dp_approx = dp_approx - min_xy

    return dp_approx, spline_pts


def export_dxf(
    dxf_path: Path,
    spline_pts_in: np.ndarray,
    dp_pts_in: np.ndarray,
    export_mode: str = "both",
) -> None:
    """
    Exports a 1:1 scale DXF file in inches ($INSUNITS = 1).
    Includes layers:
      - WINDOW_CUT_LWPOLYLINE: Closed high-resolution LWPOLYLINE (universal CNC router compatibility)
      - WINDOW_CUT_SPLINE: Closed cubic SPLINE fit
      - WINDOW_DP_APPROX: Douglas-Peucker simplified polygon reference
      - VERIFICATION_BBOX: Reference bounding box in inches
    """
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 1  # 1 = Inches
    doc.header["$MEASUREMENT"] = 0  # 0 = English (inches)

    msp = doc.modelspace()

    doc.layers.add("WINDOW_CUT_LWPOLYLINE", color=1)  # Red
    doc.layers.add("WINDOW_CUT_SPLINE", color=5)      # Blue
    doc.layers.add("WINDOW_DP_APPROX", color=3)       # Green
    doc.layers.add("VERIFICATION_BBOX", color=8)      # Gray

    if export_mode in ("lwpolyline", "both"):
        msp.add_lwpolyline(
            spline_pts_in.tolist(),
            format="xy",
            close=True,
            dxfattribs={"layer": "WINDOW_CUT_LWPOLYLINE"},
        )

    if export_mode in ("spline", "both"):
        # Downsample control/fit points for a clean closed CAD spline
        step = max(1, len(spline_pts_in) // 120)
        fit_pts = spline_pts_in[::step].tolist()
        if fit_pts[0] != fit_pts[-1]:
            fit_pts.append(fit_pts[0])
        fit_pts_3d = [(x, y, 0.0) for x, y in fit_pts]
        msp.add_spline(fit_points=fit_pts_3d, dxfattribs={"layer": "WINDOW_CUT_SPLINE"})

    msp.add_lwpolyline(
        dp_pts_in.tolist(),
        format="xy",
        close=True,
        dxfattribs={"layer": "WINDOW_DP_APPROX"},
    )

    # Add reference bounding box
    min_x, min_y = spline_pts_in.min(axis=0)
    max_x, max_y = spline_pts_in.max(axis=0)
    bbox_pts = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]
    msp.add_lwpolyline(bbox_pts, format="xy", close=True, dxfattribs={"layer": "VERIFICATION_BBOX"})

    doc.saveas(dxf_path)


def export_svg_overlay(
    svg_path: Path,
    rectified_bgr: np.ndarray,
    contour_px: np.ndarray,
    spline_pts_in: np.ndarray,
    ppi: float,
    dst_pts_in: np.ndarray,
    margin_in: float,
) -> None:
    """
    Exports a visual verification SVG containing:
      1. The rectified template image embedded as base64 PNG at exact 1:1 inch dimensions.
      2. Calibration crosshair target overlays at their exact metric grid coordinates.
      3. Raw extracted contour (dashed yellow) and final smoothed CNC cut path (solid cyan/red).
    """
    h_px, w_px = rectified_bgr.shape[:2]
    w_in = w_px / ppi
    h_in = h_px / ppi

    success, png_buf = cv2.imencode(".png", rectified_bgr)
    if not success:
        raise RuntimeError("Failed to encode rectified image for SVG overlay.")
    b64_img = base64.b64encode(png_buf.tobytes()).decode("ascii")

    # Map smoothed spline back to rectified pixel coordinates for exact overlay comparison
    raw_in_img = contour_px / ppi
    # Re-derive translation offset from raw contour vs spline
    raw_cad = raw_in_img.copy()
    raw_cad[:, 1] = -raw_cad[:, 1]
    offset_cad = raw_cad.min(axis=0)
    spline_cad = spline_pts_in + offset_cad
    spline_px = np.column_stack([spline_cad[:, 0] * ppi, -spline_cad[:, 1] * ppi])

    raw_poly_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in contour_px[::max(1, len(contour_px)//600)])
    spline_poly_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in spline_px)

    crosshair_elems = []
    for idx, pt_in in enumerate(dst_pts_in):
        cx = (pt_in[0] + margin_in) * ppi
        cy = (pt_in[1] + margin_in) * ppi
        r = 0.25 * ppi
        crosshair_elems.append(
            f'<g stroke="#00ff00" stroke-width="2" fill="none">'
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}"/>'
            f'<line x1="{cx - r*1.5:.2f}" y1="{cy:.2f}" x2="{cx + r*1.5:.2f}" y2="{cy:.2f}"/>'
            f'<line x1="{cx:.2f}" y1="{cy - r*1.5:.2f}" x2="{cx:.2f}" y2="{cy + r*1.5:.2f}"/>'
            f'<text x="{cx + 12:.2f}" y="{cy - 12:.2f}" fill="#00aa00" font-family="monospace" font-size="16">'
            f'Grid ({pt_in[0]:.1f}", {pt_in[1]:.1f}")</text>'
            f'</g>'
        )

    svg_content = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="{w_in:.4f}in"
     height="{h_in:.4f}in"
     viewBox="0 0 {w_px} {h_px}">
  <title>Cabin Window Rectified Overlay Verification</title>
  <image x="0" y="0" width="{w_px}" height="{h_px}" xlink:href="data:image/png;base64,{b64_img}" />
  <g id="calibration-grid">
    {"".join(crosshair_elems)}
  </g>
  <g id="raw-contour" fill="none" stroke="#ffaa00" stroke-width="1.5" stroke-dasharray="6,4" opacity="0.85">
    <polygon points="{raw_poly_str}" />
  </g>
  <g id="smoothed-cnc-profile" fill="none" stroke="#ff0033" stroke-width="2.5">
    <polygon points="{spline_poly_str}" />
  </g>
</svg>
"""
    svg_path.write_text(svg_content, encoding="utf-8")


def export_clean_1to1_svg(svg_path: Path, spline_pts_in: np.ndarray) -> None:
    """
    Exports a pure 1:1 vector SVG (in inches, no background raster) ready for CAM/vector import.
    Note: SVG +Y is down, so we flip Y relative to the bounding box height.
    """
    max_x, max_y = spline_pts_in.max(axis=0)
    pts_svg = np.column_stack([spline_pts_in[:, 0], max_y - spline_pts_in[:, 1]])
    poly_str = " ".join(f"{x:.5f},{y:.5f}" for x, y in pts_svg)

    svg_content = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{max_x:.5f}in"
     height="{max_y:.5f}in"
     viewBox="0 0 {max_x:.5f} {max_y:.5f}">
  <polygon points="{poly_str}" fill="none" stroke="#000000" stroke-width="0.01" />
</svg>
"""
    svg_path.write_text(svg_content, encoding="utf-8")


def compute_dimensions(spline_pts_in: np.ndarray) -> dict:
    """Computes critical physical verification dimensions in inches."""
    min_xy = spline_pts_in.min(axis=0)
    max_xy = spline_pts_in.max(axis=0)
    width_in = float(max_xy[0] - min_xy[0])
    height_in = float(max_xy[1] - min_xy[1])
    diag_in = float(np.hypot(width_in, height_in))

    # Oriented minimum-area bounding box (in case the window sits slightly rotated relative to the grid)
    cnt_f32 = spline_pts_in.reshape(-1, 1, 2).astype(np.float32)
    (_, (w_rot, h_rot), _) = cv2.minAreaRect(cnt_f32)
    min_area_len = float(max(w_rot, h_rot))
    min_area_ht = float(min(w_rot, h_rot))
    min_area_diag = float(np.hypot(min_area_len, min_area_ht))

    # Closed perimeter & area
    closed = np.vstack([spline_pts_in, spline_pts_in[0]])
    diffs = np.diff(closed, axis=0)
    perimeter_in = float(np.sum(np.hypot(diffs[:, 0], diffs[:, 1])))
    area_sq_in = float(cv2.contourArea(cnt_f32))

    return {
        "overall_length_in": width_in,
        "max_height_in": height_in,
        "bbox_diagonal_in": diag_in,
        "min_area_length_in": min_area_len,
        "min_area_height_in": min_area_ht,
        "min_area_diagonal_in": min_area_diag,
        "perimeter_in": perimeter_in,
        "area_sq_in": area_sq_in,
    }


def print_verification_readout(report: VerificationReport) -> None:
    """Prints a formatted dimensional verification readout table for shop comparison."""
    print("\n" + "=" * 72)
    print("  CABIN WINDOW CNC PROFILE - DIMENSIONAL VERIFICATION READOUT")
    print("=" * 72)
    print(f"  Source Image          : {report.image_path}")
    print(f"  Calibration Grid      : {report.grid_width_in:.2f}\" x {report.grid_height_in:.2f}\" @ {report.ppi:.0f} PPI")
    print(f"  Homography Reproj Err : {report.reprojection_error_px:.3f} px ({report.reprojection_error_px / report.ppi * 1000:.2f} mils)")
    print("-" * 72)
    print("  AXIS-ALIGNED BOUNDING DIMENSIONS (Relative to Calibration Grid):")
    print(f"    Overall Length (X)  : {report.overall_length_in:8.3f} in   ({report.overall_length_in * 25.4:8.2f} mm)")
    print(f"    Max Height     (Y)  : {report.max_height_in:8.3f} in   ({report.max_height_in * 25.4:8.2f} mm)")
    print(f"    BBox Diagonal       : {report.bbox_diagonal_in:8.3f} in   ({report.bbox_diagonal_in * 25.4:8.2f} mm)")
    print("-" * 72)
    print("  MINIMUM-AREA ORIENTED BOUNDING BOX (Rotation-Independent):")
    print(f"    Oriented Length     : {report.min_area_length_in:8.3f} in   ({report.min_area_length_in * 25.4:8.2f} mm)")
    print(f"    Oriented Height     : {report.min_area_height_in:8.3f} in   ({report.min_area_height_in * 25.4:8.2f} mm)")
    print(f"    Oriented Diagonal   : {report.min_area_diagonal_in:8.3f} in   ({report.min_area_diagonal_in * 25.4:8.2f} mm)")
    print("-" * 72)
    print(f"  Cut Perimeter Length  : {report.perimeter_in:8.3f} in   ({report.perimeter_in / 12.0:6.2f} ft)")
    print(f"  Enclosed Window Area  : {report.area_sq_in:8.2f} sq in ({report.area_sq_in / 144.0:6.2f} sq ft)")
    print("-" * 72)
    print(f"  1:1 CNC DXF Output    : {report.dxf_path}")
    print(f"  Overlay SVG Output    : {report.svg_overlay_path}")
    print("=" * 72 + "\n")


def generate_synthetic_test_template(out_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """
    Generates a realistic synthetic perspective-warped photo of a marine cabin window
    traced on paper with a 36" x 24" (multiples of 12.0") '+' crosshair grid for self-testing.
    """
    ppi = 100.0
    margin_in = 4.0
    grid_w_in, grid_h_in = 36.0, 24.0
    canvas_w = int((grid_w_in + 2 * margin_in) * ppi)
    canvas_h = int((grid_h_in + 2 * margin_in) * ppi)

    canvas = np.full((canvas_h, canvas_w, 3), 245, dtype=np.uint8)

    # Draw 4 corner crosshairs at (0,0), (36,0), (36,24), (0,24) inches
    dst_pts_in = np.array([[0.0, 0.0], [grid_w_in, 0.0], [grid_w_in, grid_h_in], [0.0, grid_h_in]], dtype=np.float32)
    for gx, gy in dst_pts_in:
        cx = int(round((gx + margin_in) * ppi))
        cy = int(round((gy + margin_in) * ppi))
        arm = int(0.4 * ppi)
        cv2.line(canvas, (cx - arm, cy), (cx + arm, cy), (30, 30, 30), 3, cv2.LINE_AA)
        cv2.line(canvas, (cx, cy - arm), (cx, cy + arm), (30, 30, 30), 3, cv2.LINE_AA)

    # Draw a known trapezoidal-curved cabin window profile: exactly 30.00" long x 14.00" high
    # Centered inside the 36" x 24" grid
    t = np.linspace(0, 2 * np.pi, 1000, endpoint=False)
    # Superellipse / rounded marine window shape with exact half-extents 15.0" and 7.0"
    a = 15.0  # total length = 30.00 in
    b = 7.0   # total height = 14.00 in
    n = 3.2
    x_in = 18.0 + a * np.sign(np.cos(t)) * (np.abs(np.cos(t)) ** (2.0 / n))
    y_in = 12.0 + b * np.sign(np.sin(t)) * (np.abs(np.sin(t)) ** (2.0 / n))

    pts_px = np.column_stack([(x_in + margin_in) * ppi, (y_in + margin_in) * ppi]).astype(np.int32)
    cv2.polylines(canvas, [pts_px.reshape(-1, 1, 2)], isClosed=True, color=(20, 20, 20), thickness=3, lineType=cv2.LINE_AA)

    # Apply a synthetic camera perspective tilt
    src_corners = (dst_pts_in + margin_in) * ppi
    warped_corners = src_corners + np.array([[80, 110], [-60, 70], [-110, -90], [50, -40]], dtype=np.float32)
    H_sim = cv2.getPerspectiveTransform(src_corners.astype(np.float32), warped_corners.astype(np.float32))
    sim_photo = cv2.warpPerspective(canvas, H_sim, (canvas_w, canvas_h), borderValue=(220, 220, 220))

    cv2.imwrite(str(out_path), sim_photo)
    return warped_corners, dst_pts_in


def process_template(
    image_path: Path,
    output_dir: Path,
    grid_width_in: float = 12.0,
    grid_height_in: float = 12.0,
    src_points: Optional[np.ndarray] = None,
    dst_points_in: Optional[np.ndarray] = None,
    interactive: bool = False,
    ppi: float = 200.0,
    margin_in: float = 4.0,
    dp_epsilon_in: float = 0.010,
    spline_smoothing: float = 0.002,
    extract_mode: str = "centerline",
) -> VerificationReport:
    """End-to-end processing pipeline for a single window template photograph."""
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"Could not read image at {image_path}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    if dst_points_in is None:
        dst_points_in = np.array(
            [
                [0.0, 0.0],
                [grid_width_in, 0.0],
                [grid_width_in, grid_height_in],
                [0.0, grid_height_in],
            ],
            dtype=np.float32,
        )

    if src_points is not None:
        src_pts = np.array([refine_crosshair_center(gray, (p[0], p[1])) for p in src_points], dtype=np.float32)
    elif interactive:
        src_pts = pick_crosshairs_interactive(image, gray, num_points=len(dst_points_in))
    else:
        src_pts = auto_detect_crosshairs(gray, expected_count=len(dst_points_in))

    rectified_bgr, _, reproj_err = rectify_image(
        image, src_pts, dst_points_in, ppi=ppi, margin_in=margin_in
    )

    contour_px = extract_window_contour(
        rectified_bgr,
        dst_points_in,
        ppi=ppi,
        margin_in=margin_in,
        extract_mode=extract_mode,
    )

    dp_pts_in, spline_pts_in = clean_and_smooth_contour(
        contour_px,
        ppi=ppi,
        dp_epsilon_in=dp_epsilon_in,
        spline_smoothing=spline_smoothing,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem
    dxf_path = output_dir / f"{stem}_cnc_1to1.dxf"
    svg_clean_path = output_dir / f"{stem}_cnc_1to1.svg"
    svg_overlay_path = output_dir / f"{stem}_verification_overlay.svg"
    rectified_png_path = output_dir / f"{stem}_rectified.png"

    export_dxf(dxf_path, spline_pts_in, dp_pts_in, export_mode="both")
    export_clean_1to1_svg(svg_clean_path, spline_pts_in)
    export_svg_overlay(
        svg_overlay_path,
        rectified_bgr,
        contour_px,
        spline_pts_in,
        ppi=ppi,
        dst_pts_in=dst_points_in,
        margin_in=margin_in,
    )
    cv2.imwrite(str(rectified_png_path), rectified_bgr)

    dims = compute_dimensions(spline_pts_in)
    report = VerificationReport(
        image_path=str(image_path),
        ppi=ppi,
        grid_width_in=float(dst_points_in[:, 0].max() - dst_points_in[:, 0].min()),
        grid_height_in=float(dst_points_in[:, 1].max() - dst_points_in[:, 1].min()),
        reprojection_error_px=reproj_err,
        dxf_path=str(dxf_path),
        svg_overlay_path=str(svg_overlay_path),
        **dims,
    )
    print_verification_readout(report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert photographed paper window templates with 12\" calibration crosshairs into 1:1 CNC DXF/SVG profiles."
    )
    parser.add_argument("images", nargs="*", help="Path(s) to input template photograph(s).")
    parser.add_argument("-o", "--output-dir", default="output", help="Directory for generated DXF/SVG files (default: output).")
    parser.add_argument("--grid-width", type=float, default=12.0, help="Horizontal spacing in inches between selected corner crosshairs (e.g. 12.0, 24.0, 36.0).")
    parser.add_argument("--grid-height", type=float, default=12.0, help="Vertical spacing in inches between selected corner crosshairs (e.g. 12.0, 24.0).")
    parser.add_argument("--src-points", type=str, default=None, help="Manual pixel coordinates of crosshairs: 'x1,y1 x2,y2 x3,y3 x4,y4' or JSON list.")
    parser.add_argument("--dst-points", type=str, default=None, help="Custom real-world inch coordinates of crosshairs (overrides --grid-width/--grid-height).")
    parser.add_argument("-i", "--interactive", action="store_true", help="Open interactive OpenCV window to click calibration crosshairs.")
    parser.add_argument("--ppi", type=float, default=200.0, help="Rectified orthographic working resolution in pixels per inch (default: 200).")
    parser.add_argument("--margin", type=float, default=4.0, help="Margin in inches around calibration grid in rectified image (default: 4.0).")
    parser.add_argument("--dp-epsilon", type=float, default=0.010, help="Douglas-Peucker approximation epsilon in inches (default: 0.010).")
    parser.add_argument("--spline-smoothing", type=float, default=0.002, help="SciPy periodic B-spline smoothing factor s (default: 0.002).")
    parser.add_argument("--extract-mode", choices=["centerline", "outer", "inner"], default="centerline", help="Which edge of the pen stroke to trace (default: centerline).")
    parser.add_argument("--self-test", action="store_true", help="Run a synthetic verification test on a known 30.00\" x 14.00\" cabin window template.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    out_dir = Path(args.output_dir)

    if args.self_test:
        test_dir = out_dir / "self_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        synth_img = test_dir / "synthetic_window_template.png"
        warped_pts, dst_pts_in = generate_synthetic_test_template(synth_img)
        report = process_template(
            image_path=synth_img,
            output_dir=test_dir,
            src_points=warped_pts,
            dst_points_in=dst_pts_in,
            ppi=args.ppi,
            margin_in=args.margin,
            dp_epsilon_in=args.dp_epsilon,
            spline_smoothing=args.spline_smoothing,
            extract_mode=args.extract_mode,
        )
        print(f"[Self-Test Target: 30.000\" x 14.000\" | Measured: {report.overall_length_in:.3f}\" x {report.max_height_in:.3f}\"]")
        return 0

    if not args.images:
        parser.print_help()
        return 1

    src_pts = parse_points_arg(args.src_points) if args.src_points else None
    dst_pts = parse_points_arg(args.dst_points) if args.dst_points else None

    for img_str in args.images:
        process_template(
            image_path=Path(img_str),
            output_dir=out_dir,
            grid_width_in=args.grid_width,
            grid_height_in=args.grid_height,
            src_points=src_pts,
            dst_points_in=dst_pts,
            interactive=args.interactive,
            ppi=args.ppi,
            margin_in=args.margin,
            dp_epsilon_in=args.dp_epsilon,
            spline_smoothing=args.spline_smoothing,
            extract_mode=args.extract_mode,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
