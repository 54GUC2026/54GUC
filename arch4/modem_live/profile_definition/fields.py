"""The one owned catalogue of canonical field meanings, types and stable IDs."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _definitions():
    return json.loads(Path(__file__).with_name('field-definitions.v2.json').read_text(encoding='utf-8'))


def canonical_fields():
    # Neither authoring tools nor a selected modem may mutate shared meanings.
    return deepcopy(_definitions())
