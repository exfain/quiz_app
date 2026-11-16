"""Finite numeric input and decimal arithmetic at scoring boundaries."""
import math
import sys
from decimal import Decimal


def finite_number(value):
    if isinstance(value, bool):
        raise ValueError('Bitte eine endliche Zahl eingeben.')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Bitte eine endliche Zahl eingeben.')
    return result


def decimal_number(value):
    return Decimal(str(finite_number(value)))


def deviation(answer, correct):
    answer, correct = decimal_number(answer), decimal_number(correct)
    if not correct:
        return Decimal(0) if not answer else Decimal(str(sys.float_info.max))
    return abs((answer - correct) / correct) * 100


def finite_percentage(answer, correct):
    return float(min(deviation(answer, correct), Decimal(str(sys.float_info.max))))


def bounded_float(value):
    limit = Decimal(str(sys.float_info.max))
    return float(max(-limit, min(value, limit)))


def display_number(value):
    try:
        text = format(decimal_number(value), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except (ValueError, TypeError, OverflowError):
        return '–'
