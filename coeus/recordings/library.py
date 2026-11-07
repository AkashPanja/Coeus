"""The recordings library: which folder under the mounted path holds the camera folders.

docker-compose mounts some host path at COEUS_RECORDINGS_DIR (e.g. /volume1/surveillance or
all of /volume1). In the UI the user picks the folder that contains the camera folders; it is
stored relative to the mount in the ``library.root`` setting.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coeus.recordings.synology import is_camera_folder, list_camera_folders

MAX_SCAN_DEPTH = 3
MAX_DIRS_SCANNED = 2000


class OutsideMount(ValueError):
    pass


@dataclass
class FolderInfo:
    name: str
    rel: str          # path relative to the mount, "" for the mount itself
    kind: str         # "library" (contains camera folders) | "camera" | "folder"
    cameras: int = 0  # camera folders directly inside (for kind == "library")


def resolve(mount: Path, rel: str) -> Path:
    """Join rel onto mount, refusing anything that escapes the mount."""
    mount = mount.resolve()
    target = (mount / (rel or "").strip("/")).resolve()
    if target != mount and mount not in target.parents:
        raise OutsideMount(f"{rel!r} is outside the mounted recordings path")
    return target


def rel_of(mount: Path, path: Path) -> str:
    rel = path.resolve().relative_to(mount.resolve()).as_posix()
    return "" if rel == "." else rel


def describe(mount: Path, path: Path) -> FolderInfo:
    rel = rel_of(mount, path)
    name = path.name if rel else "/ (mounted path)"
    if is_camera_folder(path):
        return FolderInfo(name, rel, "camera")
    n = len(list_camera_folders(path))
    return FolderInfo(name, rel, "library" if n else "folder", n)


def browse(mount: Path, rel: str) -> tuple[FolderInfo, list[FolderInfo]]:
    """The folder at rel, and its visible subfolders."""
    here = resolve(mount, rel)
    if not here.is_dir():
        raise FileNotFoundError(rel)
    children = []
    for child in sorted(here.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and not child.name.startswith((".", "@", "#")):
            try:
                children.append(describe(mount, child))
            except PermissionError:
                continue
    return describe(mount, here), children


def detect_libraries(mount: Path) -> list[FolderInfo]:
    """Folders (up to a few levels deep) that directly contain camera folders, best first."""
    found: list[FolderInfo] = []
    scanned = 0
    queue: list[tuple[Path, int]] = [(mount.resolve(), 0)]
    while queue and scanned < MAX_DIRS_SCANNED:
        path, depth = queue.pop(0)
        scanned += 1
        try:
            info = describe(mount, path)
        except PermissionError:
            continue
        if info.kind == "library":
            found.append(info)
            continue  # don't look inside a library: its children are cameras
        if info.kind == "camera" or depth >= MAX_SCAN_DEPTH:
            continue
        try:
            subdirs = [c for c in sorted(path.iterdir())
                       if c.is_dir() and not c.name.startswith((".", "@", "#"))]
        except PermissionError:
            continue
        queue.extend((c, depth + 1) for c in subdirs)
    found.sort(key=lambda f: -f.cameras)
    return found


def normalise_choice(mount: Path, rel: str) -> tuple[str, str]:
    """Turn the user's pick into a library root. Returns (root_rel, explanation)."""
    path = resolve(mount, rel)
    info = describe(mount, path)
    if info.kind == "library":
        return info.rel, f"Found {info.cameras} camera folder(s) in “{info.name}”."
    if info.kind == "camera":
        parent = describe(mount, path.parent)
        return parent.rel, (f"“{info.name}” is a single camera folder, so its parent "
                            f"“{parent.name}” is used ({parent.cameras} camera folder(s)).")
    libs = [lib for lib in detect_libraries(path) if lib.rel != info.rel]
    if len(libs) == 1:
        return libs[0].rel, (f"No camera folders directly in “{info.name}”; using "
                             f"“{libs[0].rel}” ({libs[0].cameras} camera folder(s)) inside it.")
    if libs:
        raise ValueError("Several folders inside contain cameras: "
                         + ", ".join(f"{lib.rel} ({lib.cameras})" for lib in libs[:6])
                         + ". Open the one you want and select it.")
    raise ValueError(f"No Surveillance Station camera folders found in “{info.name}”.")


def library_root(mount: Path, root_rel: str) -> Path:
    try:
        return resolve(mount, root_rel)
    except OutsideMount:
        return mount.resolve()
