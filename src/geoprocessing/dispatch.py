import os
import json


class UnsupportedAlgorithmError(Exception):
    pass


class InvalidInputFormatError(Exception):
    pass


_tools_cache = None


def get_tools():
    global _tools_cache
    if _tools_cache is None:
        with open(os.path.join(os.path.dirname(__file__), "tools.json"), "r") as f:
            _tools_cache = json.load(f)
    return _tools_cache
