"""Local geometric slide lookup; no semantic models, network, or persistent writes."""
from __future__ import annotations

import base64
import binascii
from functools import lru_cache
import io

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .. import artifacts, config
from .evidence_reader import WorkspaceError


def decode_image(raw):
    limits = config.settings.slide_match
    if not 0 < len(raw) <= limits.max_image_bytes:
        raise WorkspaceError('Image must be at most 3 MiB; resize or encode as JPEG')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG') or image.width * image.height > limits.max_image_pixels:
                raise WorkspaceError('Use a PNG/JPEG image of at most 12 million pixels')
            image = ImageOps.exif_transpose(image)
            image.thumbnail((limits.long_edge, limits.long_edge))
            return np.array(image.convert('L'))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError('Invalid or unsupported image') from None


def _features(raw, params):
    gray = decode_image(raw)
    points, descriptors = cv2.SIFT_create(nfeatures=params.max_features).detectAndCompute(gray, None)
    return np.float32([p.pt for p in points]).reshape(-1, 2), descriptors, gray.shape


@lru_cache(maxsize=256)
def _stored_features(path, modified_ns, size, params):
    # Bounded RAM cache; stat + parameters invalidate it without touching course files.
    if size > params.max_image_bytes:
        raise WorkspaceError('Stored screenshot exceeds the image size limit')
    with path.open('rb') as handle:
        return _features(handle.read(params.max_image_bytes + 1), params)


def _match(query, target, params):
    qp, qd, qs = query
    tp, td, ts = target
    if qd is None or td is None or min(len(qp), len(tp)) < params.min_inliers:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(qd, td, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < params.ratio_test * pair[1].distance]
    # Repeated glyphs must not inflate evidence by mapping many points to one point.
    unique = {}
    for match in sorted(good, key=lambda m: m.distance):
        unique.setdefault(match.trainIdx, match)
    good = list(unique.values())
    if len(good) < params.min_inliers:
        return None
    source = np.float32([qp[m.queryIdx] for m in good])
    destination = np.float32([tp[m.trainIdx] for m in good])
    matrix, mask = cv2.findHomography(source, destination, cv2.RANSAC, params.ransac_pixels)
    if matrix is None or mask is None or not np.isfinite(matrix).all():
        return None
    keep = mask.ravel().astype(bool)
    count = int(keep.sum())
    ratio = count / len(good)
    fraction = count / min(len(qp), len(tp))
    coverage = min(cv2.contourArea(cv2.convexHull(points[keep])) / (shape[0] * shape[1])
                   for points, shape in ((source, qs), (destination, ts))) if count >= 3 else 0
    if (count < params.min_inliers or ratio < params.min_inlier_ratio
            or fraction < params.min_feature_fraction or coverage < params.min_coverage):
        return None
    h, w = qs
    corners = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), matrix)
    if not np.isfinite(corners).all() or not cv2.isContourConvex(corners):
        return None
    return {'inliers': count, 'inlier_ratio': round(ratio, 4),
            'feature_fraction': round(fraction, 4), 'coverage': round(coverage, 4)}


def locate(reader, image_base64, offset=0, size=None):
    params = config.settings.slide_match
    size = size or config.settings.mcp.page_size
    if not isinstance(image_base64, str) or len(image_base64) > 4 * ((params.max_image_bytes + 2) // 3):
        raise WorkspaceError('image_base64 must contain a PNG/JPEG of at most 3 MiB')
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except (ValueError, binascii.Error):
        raise WorkspaceError('image_base64 must be raw base64, not a URL or path') from None
    query = _features(raw, params)
    reader._check_version()
    root = artifacts.video_dir(reader.video_id).resolve()
    images = {}
    for page in reader.pages.values():
        records = [page, *(s for s in reader.sections.values() if s.get('page') == page['id'])]
        for record in records:
            relative = record.get('image')
            if not relative:
                continue
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise WorkspaceError('Screenshot path is outside the video')
            images.setdefault(path, set()).add(page['id'])
    best = {}
    skipped = 0
    scanned = 0
    if query[1] is not None and len(query[0]) >= params.min_inliers:
        for path, pages in images.items():
            try:
                stat = path.stat()
                target = _stored_features(path, stat.st_mtime_ns, stat.st_size, params)
            except (OSError, WorkspaceError):
                skipped += 1
                continue
            scanned += 1
            quality = _match(query, target, params)
            if quality:
                for page in pages:
                    if page not in best or quality['inliers'] > best[page]['inliers']:
                        best[page] = quality
    matches = []
    for page, quality in sorted(best.items(), key=lambda item: (-item[1]['inliers'], item[0])):
        for section in sorted(reader.sections.values(), key=lambda s: (s['jump'], s['id'])):
            if section.get('page') == page:
                matches.append({'page_id': page, 'unit_id': section['id'], 'timestamp': section['jump'],
                                'end': reader._end(section), 'match': quality})
    reader._check_version()
    end = offset + size
    return {'matches': matches[offset:end], 'total_matches': len(matches),
            'next_offset': end if end < len(matches) else None,
            'candidate_pages': len(best), 'scanned_images': scanned, 'skipped_images': skipped,
            'query_features': len(query[0]), 'method': 'sift_ransac',
            'note': 'Geometric candidates, not semantic search or proof of page identity. '
                    'Call get_video_units at timestamp for a preview; read_video_unit/view_slide only as needed. '
                    'Multiple pages can be similar; repeated appearances remain separate.'}
