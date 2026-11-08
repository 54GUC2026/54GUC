"""Authoring facade for the single runtime-owned semantic catalogue."""
from arch4.modem_live.profile_definition.fields import canonical_fields


def catalogue_v2():
    # V2 files are retained migration evidence, not the live R25 format. Their
    # original complete155-slot contract stays fixed; meanings share one owner.
    return [field for field in canonical_fields()if field['id']<=155]
