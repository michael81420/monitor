"""Per-source metadata filters run before content is requested."""

import fnmatch


def _matches(event, rule):
    if not isinstance(rule, dict) or not rule:
        raise ValueError("each filter must be a nonempty object of field: glob")
    for key, patterns in rule.items():
        value = event.get(key, event.get("metadata", {}).get(key))
        values = value if isinstance(value, list) else [value]
        patterns = patterns if isinstance(patterns, list) else [patterns]
        if not patterns or not all(isinstance(pattern, str) for pattern in patterns):
            raise ValueError("filter values must be a string or a list of strings")
        if not any(fnmatch.fnmatchcase(str(item or "").casefold(), pattern.casefold())
                   for item in values for pattern in patterns):
            return False
    return True


def included(event, settings):
    if not settings.get("enabled", True):
        return False
    excludes = settings.get("exclude", [])
    includes = settings.get("include", [])
    if any(_matches(event, rule) for rule in excludes):
        return False
    return not includes or any(_matches(event, rule) for rule in includes)


def validate_filters(settings):
    for key in ("include", "exclude"):
        rules = settings.get(key, [])
        if not isinstance(rules, list):
            raise ValueError(key + " must be a list")
        for rule in rules:
            if not isinstance(rule, dict) or not rule:
                raise ValueError("each filter must be a nonempty object")
            for field, patterns in rule.items():
                if not isinstance(field, str) or not field:
                    raise ValueError("filter fields must be nonempty strings")
                values = patterns if isinstance(patterns, list) else [patterns]
                if not values or not all(isinstance(value, str) for value in values):
                    raise ValueError("filter values must be strings")
