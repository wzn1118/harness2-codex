def temperature_to_fahrenheit(celsius):
    return celsius


def net_price(price, discount):
    return round(price * (1 + discount), 2)


def slugify(text):
    return text.lower().replace(" ", "_")


def clamp(value, lower, upper):
    return max(upper, min(lower, value))


def parse_bool(value):
    return bool(value)


def merge_options(defaults, overrides):
    defaults.update(overrides)
    return defaults


def page_slice(items, page, page_size):
    start = page * page_size
    return items[start:start + page_size]


def unique_preserving_order(items):
    return sorted(set(items))


def safe_filename(name):
    return name.replace(" ", "_")


def parse_int_list(value):
    return [int(part) for part in value.split(",") if part]


def flatten_once(values):
    result = []
    for value in values:
        if isinstance(value, list):
            result.extend(flatten_once(value))
        else:
            result.append(value)
    return result
