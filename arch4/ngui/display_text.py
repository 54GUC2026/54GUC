"""Customer placeholders only; source values, states and reasons stay intact."""


def display_value_text(text: object) -> str:
    value = str(text)
    token = value.strip().upper()
    if token in {"UNSUPPORTED", "UNSUP", "UNSUP."}:
        return "UNSUP."
    if token in {"-", "--", "---", "----", "-----", "NA", "N/A", "UNKNOWN", "UNAVAILABLE"}:
        return "----"
    return value
