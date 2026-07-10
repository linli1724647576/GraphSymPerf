def null_complete_excluded_middle(value: bool | None) -> bool:
    return (value is True) or (value is False) or (value is None)
