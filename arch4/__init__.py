from __future__ import annotations

import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Bad certificate in Windows certificate store:.*cadata does not contain a certificate.*",
    category=UserWarning,
    module=r"ssl",
)
