"""Reject malformed and mismatched release manifests before running an image."""
import pytest
from scripts.cloud_host import validate_manifest

REVISION = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def test_manifest_pins_digest():
    assert validate_manifest({'revision': REVISION, 'digest': DIGEST}, REVISION, 'registry/repo') == 'registry/repo@' + DIGEST


@pytest.mark.parametrize('manifest,revision', [
    ({'revision': 'c' * 40, 'digest': DIGEST}, REVISION),
    ({'revision': REVISION, 'digest': 'latest'}, REVISION),
    ({'revision': REVISION, 'digest': DIGEST + '; command'}, REVISION),
    ({'revision': '../other', 'digest': DIGEST}, '../other'),
])
def test_invalid_manifest_rejected(manifest, revision):
    with pytest.raises(ValueError):
        validate_manifest(manifest, revision, 'registry/repo')
