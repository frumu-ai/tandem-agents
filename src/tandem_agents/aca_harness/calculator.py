"""Pure calculator helpers used by the deterministic ACA harness."""


def add(left, right):
    """Return the sum of *left* and *right*."""
    return left + right


def subtract(left, right):
    """Return *right* subtracted from *left*."""
    return left - right


def multiply(left, right):
    """Return the product of *left* and *right*."""
    return left * right


def describe_operation(name, left, right):
    """Describe and execute a supported calculator operation.

    Supported operation names are ``"add"``, ``"subtract"``, and
    ``"multiply"``. Unknown operation names raise ``ValueError`` so callers
    can reliably detect unsupported requests.
    """
    if name == "add":
        result = add(left, right)
        return f"{left} + {right} = {result}"
    if name == "subtract":
        result = subtract(left, right)
        return f"{left} - {right} = {result}"
    if name == "multiply":
        result = multiply(left, right)
        return f"{left} * {right} = {result}"
    raise ValueError(f"unknown operation: {name}")
