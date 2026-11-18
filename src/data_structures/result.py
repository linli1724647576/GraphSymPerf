def freeze(value, normalize=None):
    """Freeze ordinary values; adapters decode native entities."""
    if value is None:
        return ("null",)
    # bool is an int subclass; preserve the observed value type before comparison.
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, str):
        return ("string", value)
    convert = normalize or freeze
    # Containers recurse through the adapter so nested entities keep their identity.
    if isinstance(value, (list, tuple)):
        return ("list", tuple(convert(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((key, convert(item)) for key, item in value.items())))
    raise TypeError(f"Unsupported result type: {type(value).__name__}")
