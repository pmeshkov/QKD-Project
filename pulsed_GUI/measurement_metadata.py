"""Operator-entered context saved with acquisitions, never inferred from counts."""

SAMPLE_TYPES = ["Unspecified", "Emitter", "Bright spot for testing"]
FORM = [
    ("sample-type", "Sample type (operator description)", "Unspecified", SAMPLE_TYPES),
    ("bandpass-filter", "Bandpass filter (model / center wavelength / bandwidth)", "", None),
    ("laser-power", "Laser power (include units and measurement location)", "", None),
]


def read(values):
    sample = values.get("sample-type", "Unspecified")
    if sample not in SAMPLE_TYPES:
        raise ValueError("Select the sample type.")
    return dict(sample_type=sample, bandpass_filter=str(values.get("bandpass-filter", "")).strip(),
                laser_power=str(values.get("laser-power", "")).strip())


def add_arguments(parser):
    for key, label, default, choices in FORM:
        parser.add_argument("--" + key, default=default, choices=choices, help=label)


def from_arguments(args):
    return read({key: getattr(args, key.replace("-", "_")) for key, *_ in FORM})
