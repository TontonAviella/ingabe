import asyncio

from src.tools import search_place

KIGALI = [{"lat": "-1.95", "lon": "30.06", "boundingbox": ["-2.1", "-1.8", "29.9", "30.3"], "display_name": "Kigali"}]


def _ask(query):
    return asyncio.run(search_place.search_location(search_place.SearchLocationArgs(query=query), None))


def test_a_place_is_asked_of_openstreetmap_once_then_read_from_disk(monkeypatch):
    asked = []

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return KIGALI

    def get(url, params, headers, timeout):
        asked.append(params["q"])
        return _Response()

    monkeypatch.setattr(search_place.requests, "get", get)
    first = _ask("Kigali")
    again = _ask("  kigali ")  # the same place, written differently
    assert asked == ["Kigali"]
    assert again == first and first["bbox"] == [29.9, -2.1, 30.3, -1.8]


def test_an_answer_older_than_30_days_is_asked_again(monkeypatch):
    asked = []

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return KIGALI

    monkeypatch.setattr(search_place.requests, "get", lambda url, params, headers, timeout: asked.append(1) or _Response())
    _ask("Huye")
    now = search_place.time.time()
    monkeypatch.setattr(search_place.time, "time", lambda: now + 31 * 24 * 3600)
    _ask("Huye")
    assert len(asked) == 2


def test_no_answer_is_not_kept(monkeypatch):
    asked = []

    class _Empty:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(search_place.requests, "get", lambda url, params, headers, timeout: asked.append(1) or _Empty())
    assert _ask("Nowhereville")["status"] == "error"
    _ask("Nowhereville")
    assert len(asked) == 2
