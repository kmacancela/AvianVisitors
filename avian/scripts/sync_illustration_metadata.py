#!/usr/bin/env python3
"""Sync bird illustration metadata used by the collage.

Atlas cards can display any PNG in avian/assets/illustrations, but the
collage also needs a compact alpha mask and scaled dimensions so birds can
be packed by silhouette instead of by rectangle. This helper makes that step
explicit.

Typical use after adding one bird manually:

    python3 avian/scripts/sync_illustration_metadata.py --slug chaetura-pelagica

With no --slug, the script scans for primary illustration PNGs that are
missing metadata and adds only those entries.
"""
from __future__ import annotations

import argparse
import base64
import filecmp
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

try:
    from PIL import Image, ImageFilter
except ImportError as exc:  # pragma: no cover - helpful on fresh Pis
    raise SystemExit(
        "error: Pillow is required for mask generation. Install it with "
        "`python3 -m pip install Pillow`."
    ) from exc


REPO_ROOT = Path(__file__).resolve().parents[2]
ILLUSTRATIONS = REPO_ROOT / "avian" / "assets" / "illustrations"
FRONTEND = REPO_ROOT / "avian" / "frontend"
MIRROR_PUBLIC = REPO_ROOT / "netlify-mirror" / "public"
MIRROR_ILLUSTRATIONS = MIRROR_PUBLIC / "avian" / "assets" / "illustrations"
APT_FILES = [
    FRONTEND / "apt.js",
    MIRROR_PUBLIC / "apt.js",
]
INDEX_FILES = [
    FRONTEND / "index.html",
    MIRROR_PUBLIC / "index.html",
]
MAX_DIM = 560
MASK_MAX_DIM = 93
ALPHA_THRESHOLD = 8


def is_pose_slug(slug: str) -> bool:
    return bool(re.search(r"-[0-9]+$", slug))


def primary_slugs() -> list[str]:
    slugs = []
    for path in sorted(ILLUSTRATIONS.glob("*.png")):
        slug = path.stem
        if not is_pose_slug(slug):
            slugs.append(slug)
    return slugs


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> bool:
    ordered = {key: data[key] for key in sorted(data)}
    text = json.dumps(ordered, separators=(",", ":")) + "\n"
    old = path.read_text() if path.exists() else ""
    if old == text:
        return False
    path.write_text(text)
    return True


def illustration_files_for_slug(slug: str) -> list[Path]:
    files = []
    primary = ILLUSTRATIONS / f"{slug}.png"
    if primary.exists():
        files.append(primary)
    files.extend(sorted(ILLUSTRATIONS.glob(f"{slug}-[0-9]*.png")))
    return files


def copy_if_changed(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and filecmp.cmp(src, dst, shallow=False):
        return False
    shutil.copy2(src, dst)
    return True


def build_metadata(path: Path) -> tuple[list[int], dict]:
    image = Image.open(path).convert("RGBA")
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise ValueError(f"{path} has no visible alpha pixels")

    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    scale = MAX_DIM / max(width, height)
    dims = [round(width * scale), round(height * scale)]

    mask_scale = MASK_MAX_DIM / max(width, height)
    mask_w = max(1, round(width * mask_scale))
    mask_h = max(1, round(height * mask_scale))
    mask_alpha = alpha.crop(bbox).resize((mask_w, mask_h), Image.Resampling.LANCZOS)
    mask_alpha = mask_alpha.point(
        lambda pixel: 255 if pixel > ALPHA_THRESHOLD else 0,
        "L",
    )
    # A small dilation bakes in a visual gap so packed birds do not touch.
    mask_alpha = mask_alpha.filter(ImageFilter.MaxFilter(3))

    packed = bytearray((mask_w * mask_h + 7) // 8)
    for y in range(mask_h):
        for x in range(mask_w):
            if mask_alpha.getpixel((x, y)) <= 0:
                continue
            idx = y * mask_w + x
            packed[idx >> 3] |= 1 << (7 - (idx & 7))

    mask = {
        "bits": base64.b64encode(packed).decode("ascii"),
        "h": mask_h,
        "w": mask_w,
    }
    return dims, mask


def replace_js_object(text: str, name: str, value: dict) -> str:
    encoded = json.dumps(value, separators=(",", ":"))
    pattern = rf"var {re.escape(name)} = \{{.*?\}};"
    replacement = f"var {name} = {encoded};"
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise ValueError(f"could not find var {name} in apt.js")
    return new


def bump_numeric_version(text: str, name: str) -> str:
    pattern = rf"(var {re.escape(name)} = ')([0-9]+)(';)"

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{int(match.group(2)) + 1}{match.group(3)}"

    new, count = re.subn(pattern, repl, text, count=1)
    if count != 1:
        raise ValueError(f"could not find numeric {name} in apt.js")
    return new


def update_apt_files(dims: dict, masks: dict, bump_asset_versions: bool) -> bool:
    changed = False
    for path in APT_FILES:
        text = path.read_text()
        new = replace_js_object(text, "DIMS", dims)
        new = replace_js_object(new, "MASKS", masks)
        if bump_asset_versions:
            new = bump_numeric_version(new, "SKETCH_VERSION")
            new = bump_numeric_version(new, "IMG_VERSION")
        if new != text:
            path.write_text(new)
            changed = True
    return changed


def bump_index_cache_tag() -> bool:
    tag = f"{date.today():%Y%m%d}-illustration-metadata-v1"
    changed = False
    for path in INDEX_FILES:
        if not path.exists():
            continue
        text = path.read_text()
        new, count = re.subn(r"(apt\.js\?v=)[^\"']+", rf"\g<1>{tag}", text, count=1)
        if count != 1:
            raise ValueError(f"could not find apt.js cache tag in {path}")
        if new != text:
            path.write_text(new)
            changed = True
    return changed


def resolve_slugs(requested: list[str], include_all: bool) -> list[str]:
    if requested:
        return sorted(set(requested))
    all_slugs = primary_slugs()
    if include_all:
        return all_slugs
    dims = read_json(FRONTEND / "dims.json")
    masks = read_json(FRONTEND / "masks.json")
    return [slug for slug in all_slugs if slug not in dims or slug not in masks]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", action="append", default=[],
                        help="Primary species slug to refresh, e.g. chaetura-pelagica. Repeatable.")
    parser.add_argument("--all", action="store_true",
                        help="Refresh metadata for every primary illustration PNG.")
    parser.add_argument("--no-mirror-copy", action="store_true",
                        help="Do not copy illustration PNGs into netlify-mirror/public.")
    parser.add_argument("--no-version-bump", action="store_true",
                        help="Do not bump image/script cache versions.")
    parser.add_argument("--check", action="store_true",
                        help="Report missing metadata or mirror assets without writing files.")
    args = parser.parse_args()

    slugs = resolve_slugs(args.slug, args.all)
    if not slugs:
        print("metadata already covers every primary illustration")
        return 0

    dims_path = FRONTEND / "dims.json"
    masks_path = FRONTEND / "masks.json"
    dims = read_json(dims_path)
    masks = read_json(masks_path)

    missing = []
    metadata_changed = False
    mirror_changed = False

    for slug in slugs:
        primary = ILLUSTRATIONS / f"{slug}.png"
        if not primary.exists():
            missing.append(str(primary))
            continue
        new_dims, new_mask = build_metadata(primary)
        if dims.get(slug) != new_dims:
            metadata_changed = True
            if not args.check:
                dims[slug] = new_dims
        if masks.get(slug) != new_mask:
            metadata_changed = True
            if not args.check:
                masks[slug] = new_mask

        if not args.no_mirror_copy:
            for src in illustration_files_for_slug(slug):
                dst = MIRROR_ILLUSTRATIONS / src.name
                if args.check:
                    if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
                        mirror_changed = True
                    continue
                mirror_changed = copy_if_changed(src, dst) or mirror_changed

    if missing:
        for path in missing:
            print(f"missing illustration: {path}", file=sys.stderr)
        return 1

    if args.check:
        if metadata_changed or mirror_changed:
            print("illustration metadata is out of sync")
            return 1
        print("illustration metadata is in sync")
        return 0

    wrote_dims = write_json(dims_path, dims)
    wrote_masks = write_json(masks_path, masks)
    wrote_json = wrote_dims or wrote_masks
    bump_versions = (metadata_changed or mirror_changed) and not args.no_version_bump
    wrote_apt = update_apt_files(dims, masks, bump_versions)
    wrote_index = bump_index_cache_tag() if wrote_apt and not args.no_version_bump else False

    print(
        "synced {count} slug(s): metadata={metadata} mirror={mirror} "
        "apt={apt} cache={cache}".format(
            count=len(slugs),
            metadata=wrote_json,
            mirror=mirror_changed,
            apt=wrote_apt,
            cache=wrote_index,
        )
    )
    for slug in slugs:
        print(f"  {slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
