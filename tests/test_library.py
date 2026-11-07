from datetime import datetime

import pytest

from coeus.recordings import library
from tests.conftest import make_segment


@pytest.fixture
def mount(tmp_path):
    """/volume1-like mount with the surveillance share one level down."""
    m = tmp_path / "volume1"
    share = m / "surveillance"
    for cam in ("Main Gate", "Main Road Cam", "Hall 2 Second Floor"):
        make_segment(share, cam, datetime(2025, 5, 16, 12, 27, 34))
    (share / "@DetectionEvent").mkdir()
    (m / "docker" / "coeus").mkdir(parents=True)
    (m / "photo").mkdir()
    return m


def test_detects_surveillance_share(mount):
    libs = library.detect_libraries(mount)
    assert [(l.rel, l.cameras) for l in libs] == [("surveillance", 3)]


def test_browse_marks_folders(mount):
    here, children = library.browse(mount, "")
    assert here.kind == "folder"
    kinds = {c.name: c.kind for c in children}
    assert kinds == {"docker": "folder", "photo": "folder", "surveillance": "library"}
    _, cams = library.browse(mount, "surveillance")
    assert {c.kind for c in cams} == {"camera"}  # @DetectionEvent hidden


def test_normalise_choice_figures_it_out(mount):
    assert library.normalise_choice(mount, "surveillance")[0] == "surveillance"
    root, why = library.normalise_choice(mount, "surveillance/Main Gate")
    assert root == "surveillance" and "single camera folder" in why
    root, why = library.normalise_choice(mount, "")  # picked the mount itself
    assert root == "surveillance" and "inside it" in why
    with pytest.raises(ValueError, match="No Surveillance Station"):
        library.normalise_choice(mount, "photo")


def test_refuses_paths_outside_mount(mount):
    with pytest.raises(library.OutsideMount):
        library.resolve(mount, "../../etc")
    assert library.library_root(mount, "../..") == mount.resolve()
