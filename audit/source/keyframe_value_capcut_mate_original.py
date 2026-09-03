POSITION_X = "KFTypePositionX"
POSITION_Y = "KFTypePositionY"
_NORMALIZED_POSITION_MAX = 1.0


def normalize_keyframe_value(
    ctype: str,
    value: float,
    width: int | None = None,
    height: int | None = None,
    *,
    assume_pixel: bool = False,
) -> float:
    if ctype == POSITION_X and width is not None and width > 0:
        if assume_pixel or abs(value) > _NORMALIZED_POSITION_MAX:
            return value / width
    elif ctype == POSITION_Y and height is not None and height > 0:
        if assume_pixel or abs(value) > _NORMALIZED_POSITION_MAX:
            return value / height
    return value
