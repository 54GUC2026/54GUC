"""Fixed decoding operations inside ModemLive, never profile-supplied programs.

External profiles select fields and parameters. These reviewed data-format
implementations ship with the application, like its other parser operations.
There is no discovery of codec plugins or paths supplied by a profile.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

FAMILY_DECODERS = {
    'huawei_hilink_like':'huawei-hilink.v1',
    'routeros_at_like':'routeros.v1',
    'tplink_dev2_like':'tplink-dev2.v1',
    'xiaomi_miwifi_like':'xiaomi-miwifi.v1',
    'zte_webui_like':'zte-goform.v1',
    'zte_zwrt_ubus_like':'zte-zwrt.v1',
}

# R25.6 read extensions are admitted only by profiles that use them. Keep the
# complete prior library for legacy profiles: adding an unused 5G function
# must not change their saved interpretation or invalidate older bundles.
_OPTIONAL_READ_FUNCTIONS = frozenset({
    'huawei_optional_nr_read', 'parse_routeros_nr_monitor_row',
    '_routeros_nr_metric', 'parse_routeros_nr_qeng',
    'parse_routeros_nr_qcainfo_ul',
})


@lru_cache(maxsize=6)
def _reviewed(decoder):
    if decoder not in FAMILY_DECODERS.values():
        raise ValueError('/decoder: unknown ModemLive decoder')
    path=Path(__file__).with_name('codecs')/(decoder+'.json')
    return json.loads(path.read_text(encoding='utf-8'))


def decoder_library(decoder, *, family, expressions=None):
    if FAMILY_DECODERS.get(family)!=decoder:
        raise ValueError('/decoder: decoder does not match the selected profile family')
    # The evaluator cannot mutate another profile's shared implementation.
    library=deepcopy(_reviewed(decoder))
    if expressions is not None:
        from .vocabulary import prune_algorithms
        used=prune_algorithms(library,expressions)['functions']
        library['functions']={name:value for name,value in library['functions'].items()
                              if name not in _OPTIONAL_READ_FUNCTIONS or name in used}
    return library
