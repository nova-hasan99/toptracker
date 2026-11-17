
def is_overtime(total_seconds: int, threshold_hours: float = 8.0) -> bool:
    """Return True if a duration exceeds the given daily-hours threshold.

    Complements is_business_hours()/is_weekend() for reports flagging
    sessions that ran longer than a typical workday.
    """
    return seconds_to_hours(total_seconds) > threshold_hours
