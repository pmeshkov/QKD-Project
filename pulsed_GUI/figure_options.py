"""Shared raster export setting; never changes timestamp binning or analysis."""

FIELD = ("plot-dpi", "Saved figure resolution (DPI; 300 standard, 600 high)", "300", None)


def validated_dpi(value=300):
    try:
        dpi = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Figure DPI must be a whole number from 72 to 1200.") from None
    if str(dpi) != str(value).strip() or not 72 <= dpi <= 1200:
        raise ValueError("Figure DPI must be a whole number from 72 to 1200.")
    return dpi
