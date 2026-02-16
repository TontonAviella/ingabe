import os
import json


class UnsupportedAlgorithmError(Exception):
    pass


class InvalidInputFormatError(Exception):
    pass


def get_tools():
    with open(os.path.join(os.path.dirname(__file__), "tools.json"), "r") as f:
        return json.load(f)
