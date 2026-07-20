from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
DESKEW_MIN_ANGLE = 1.2
DESKEW_MAX_ANGLE = 12.0
LOW_TEXT_COVERAGE = 0.015
HIGH_GLARE_RATIO = 0.28
HIGH_DARK_RATIO = 0.45
LOW_BLUR_SCORE = 25.0
HIGH_CLIPPING_RATIO = 0.22


def prepare_document_page(file_path: str) -> dict[str, Any]:
    path = Path(file_path)
    if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        return {
            "prepared_path": None,
            "transformations": [],
            "quality": {"preparation_skipped": True},
        }

    from PIL import Image, ImageOps

    image = Image.open(path)
    original_size = image.size
    transformations: list[str] = []

    transposed = ImageOps.exif_transpose(image)
    if transposed.size != image.size or transposed.getexif().get(274) in (None, 1):
        # EXIF transpose is idempotent and clears an applied orientation marker.
        if image.getexif().get(274) not in (None, 1):
            transformations.append("exif_orientation")
    image = transposed.convert("RGB")

    cropped = _perspective_crop(image)
    if cropped is not None:
        image = cropped
        transformations.append("document_perspective_crop")

    rotation = _quarter_turn_rotation(image)
    if rotation:
        image = image.rotate(rotation, expand=True)
        transformations.append("auto_rotate_90_ccw" if rotation == 90 else "auto_rotate_90_cw")

    deskewed, deskew_angle = _deskew_image(image)
    if deskew_angle is not None:
        image = deskewed
        transformations.append(f"deskew_{deskew_angle:+.2f}deg")

    image = ImageOps.autocontrast(image, cutoff=1)
    transformations.append("autocontrast")

    max_dimension = max(image.size)
    if max_dimension < 1800:
        scale = min(2.0, 1800 / max_dimension)
        target_size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(target_size, Image.Resampling.LANCZOS)
        transformations.append(f"upscale_{scale:.2f}x")

    target_dir = Path(settings.uploaded_invoices_dir) / ".prepared"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{path.stem}-{uuid4().hex[:10]}.jpg"
    image.save(target_path, format="JPEG", quality=95, optimize=True)

    quality = _quality_metrics(image)
    quality["quarter_turn_applied"] = bool(rotation)
    quality["deskew_angle_applied"] = deskew_angle
    quality["original_width"] = original_size[0]
    quality["original_height"] = original_size[1]
    review_reasons, stop_reasons = _quality_review_reasons(quality)
    quality["review_reasons"] = review_reasons
    quality["stop_reasons"] = stop_reasons
    quality["requires_review"] = bool(review_reasons)
    quality["stop_recommended"] = bool(stop_reasons)
    return {
        "prepared_path": str(target_path),
        "transformations": transformations,
        "quality": quality,
    }


def _perspective_crop(image):
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return None

    array = np.array(image)
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    image_area = image.width * image.height
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        if cv2.contourArea(contour) < image_area * 0.45:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(polygon) != 4:
            continue
        points = _order_points(polygon.reshape(4, 2).astype("float32"))
        top_left, top_right, bottom_right, bottom_left = points
        width = int(
            max(
                _distance(bottom_right, bottom_left),
                _distance(top_right, top_left),
            )
        )
        height = int(
            max(
                _distance(top_right, bottom_right),
                _distance(top_left, bottom_left),
            )
        )
        if width < 200 or height < 200:
            continue
        destination = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
            dtype="float32",
        )
        transform = cv2.getPerspectiveTransform(points, destination)
        warped = cv2.warpPerspective(array, transform, (width, height))
        return Image.fromarray(warped)
    return None


def _quality_metrics(image) -> dict[str, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {
            "width": image.width,
            "height": image.height,
            "quality_metrics_available": False,
        }

    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    glare_ratio = float((gray >= 248).mean())
    dark_ratio = float((gray <= 12).mean())
    clipping_ratio = float(((gray <= 4) | (gray >= 251)).mean())
    text_coverage_ratio = float((gray < 200).mean())
    horizontal_score = float(np.std(np.mean(gray < 180, axis=1)))
    vertical_score = float(np.std(np.mean(gray < 180, axis=0)))
    return {
        "width": image.width,
        "height": image.height,
        "blur_score": round(blur_score, 2),
        "glare_ratio": round(glare_ratio, 4),
        "dark_ratio": round(dark_ratio, 4),
        "clipping_ratio": round(clipping_ratio, 4),
        "text_coverage_ratio": round(text_coverage_ratio, 4),
        "possible_quarter_turn": vertical_score > horizontal_score * 1.2,
        "quality_metrics_available": True,
    }


def _quarter_turn_rotation(image) -> int | None:
    try:
        import numpy as np
    except ImportError:
        return None

    gray = np.array(image.convert("L"))
    dark = gray < 180
    horizontal_score = float(np.std(np.mean(dark, axis=1)))
    vertical_score = float(np.std(np.mean(dark, axis=0)))
    if vertical_score <= horizontal_score * 1.2:
        return None

    return 90


def _deskew_image(image):
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return image, None

    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    _, threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(threshold > 0))
    if coords.size == 0:
        return image, None

    angle = float(cv2.minAreaRect(coords)[-1])
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < DESKEW_MIN_ANGLE or abs(angle) > DESKEW_MAX_ANGLE:
        return image, None

    center = (image.width / 2, image.height / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        np.array(image),
        matrix,
        (image.width, image.height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return Image.fromarray(rotated), round(angle, 2)


def _quality_review_reasons(quality: dict[str, Any]) -> tuple[list[str], list[str]]:
    if not quality.get("quality_metrics_available"):
        return [], []

    review_reasons: list[str] = []
    stop_reasons: list[str] = []

    if quality.get("blur_score", 0.0) < LOW_BLUR_SCORE:
        review_reasons.append("Низкая резкость страницы, OCR и vision могут ошибаться.")
    if quality.get("glare_ratio", 0.0) > HIGH_GLARE_RATIO:
        review_reasons.append("На странице много бликов.")
    if quality.get("dark_ratio", 0.0) > HIGH_DARK_RATIO:
        review_reasons.append("Страница слишком темная или сильно зачернена.")
    if quality.get("clipping_ratio", 0.0) > HIGH_CLIPPING_RATIO:
        review_reasons.append("По краям или в светах/тенях есть сильное clipping-искажение.")
    if quality.get("text_coverage_ratio", 0.0) < LOW_TEXT_COVERAGE:
        review_reasons.append("На изображении мало текстовых областей после подготовки.")

    if (
        quality.get("text_coverage_ratio", 0.0) < LOW_TEXT_COVERAGE
        and (quality.get("glare_ratio", 0.0) > HIGH_GLARE_RATIO or quality.get("blur_score", 0.0) < LOW_BLUR_SCORE)
    ):
        stop_reasons.append("Качество страницы слишком низкое для надежного извлечения.")

    return review_reasons, stop_reasons


def _order_points(points):
    import numpy as np

    ordered = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1)
    ordered[0] = points[sums.argmin()]
    ordered[2] = points[sums.argmax()]
    ordered[1] = points[differences.argmin()]
    ordered[3] = points[differences.argmax()]
    return ordered


def _distance(first, second) -> float:
    import numpy as np

    return float(np.linalg.norm(first - second))
