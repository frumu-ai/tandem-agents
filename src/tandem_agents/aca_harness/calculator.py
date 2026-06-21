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


def divide(left, right):
    """Return *left* divided by *right*.

    Raises:
        ZeroDivisionError: If division by *right* is invalid because it is zero.
    """
    if right == 0:
        raise ZeroDivisionError("cannot divide by zero")
    return left / right


_OPERATION_REGISTRY = {
    "add": (add, "+"),
    "divide": (divide, "/"),
    "multiply": (multiply, "*"),
    "subtract": (subtract, "-"),
}


def available_operations():
    """Return a sorted tuple of supported operation names."""
    return tuple(sorted(_OPERATION_REGISTRY))


def describe_operation(name, left, right):
    """Describe and execute a supported calculator operation.

    Supported operation names are ``"add"``, ``"subtract"``, ``"multiply"``,
    and ``"divide"``. Unknown operation names raise ``ValueError`` so callers
    can reliably detect unsupported requests.
    """
    try:
        operation, symbol = _OPERATION_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown operation: {name}") from exc
    result = operation(left, right)
    return f"{left} {symbol} {right} = {result}"
