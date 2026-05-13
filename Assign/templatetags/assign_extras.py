from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary or list by key/index"""
    try:
        if isinstance(dictionary, dict):
            return dictionary.get(str(key))
        elif isinstance(dictionary, (list, tuple)):
            return dictionary[int(key)]
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    return None

@register.filter
def dict_item(dictionary, key):
    """Get an item from a dictionary"""
    if isinstance(dictionary, dict):
        return dictionary.get(str(key))
    return None