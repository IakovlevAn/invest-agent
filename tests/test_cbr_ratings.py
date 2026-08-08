from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from invest_agent.http import HttpResponse
from invest_agent.ratings.cbr import (
    CbrRatingsCaptchaRequired,
    CbrRatingsClient,
    CbrRatingsContractError,
)

FIXTURES = Path(__file__).parent / "fixtures"
CSRF = "a" * 32


def response(payload: dict[str, Any]) -> HttpResponse:
    return HttpResponse(status=200, body=json.dumps(payload).encode())


def item(number: int) -> dict[str, Any]:
    return {
        "ratingAction": "Рейтинг подтвержден",
        "releaseDate": f"{number:02d}.01.2026",
        "inn": "1234567890",
        "objectType": "BNFC – нефинансовая компания",
        "ratingValue": "ruBBB+",
        "prediction": "Стабильный",
        "objectName": "Тестовый эмитент",
        "kraName": "Тестовое КРА",
        "releaseUrl": "https://agency.example/release",
        "objectId": str(number),
        "isin": "",
        "subjectName": "",
    }


class FakeSession:
    def __init__(self, *, homepage: str | None = None) -> None:
        self.homepage = homepage or f'<script>window.config={{"bitrix_sessid":"{CSRF}"}}</script>'
        self.get_calls = 0
        self.posts: list[tuple[str, dict[str, str], dict[str, str | int]]] = []

    def get(self, url: str, *, headers: Any, timeout_seconds: float) -> HttpResponse:
        self.get_calls += 1
        return HttpResponse(status=200, body=self.homepage.encode())

    def post_form(
        self,
        url: str,
        *,
        headers: Any,
        fields: Any,
        timeout_seconds: float,
    ) -> HttpResponse:
        copied_headers = dict(headers)
        copied_fields = dict(fields)
        self.posts.append((url, copied_headers, copied_fields))
        if "action=searchRatingNavigation" not in url:
            return response(
                {
                    "status": "success",
                    "data": {"itemCount": 11, "itemList": [item(1)]},
                    "errors": [],
                }
            )
        page_number = int(copied_fields["fields[pageNumber]"])
        values = [item(number) for number in (range(1, 11) if page_number == 1 else [11])]
        return response(
            {
                "status": "success",
                "data": {
                    "pageCount": 2,
                    "pageNumber": page_number,
                    "pageSize": 10,
                    "itemCount": 11,
                    "itemList": values,
                },
                "errors": [],
            }
        )


class FixtureSession(FakeSession):
    def post_form(
        self,
        url: str,
        *,
        headers: Any,
        fields: Any,
        timeout_seconds: float,
    ) -> HttpResponse:
        copied_headers = dict(headers)
        copied_fields = dict(fields)
        self.posts.append((url, copied_headers, copied_fields))
        if "action=searchRatingNavigation" in url:
            return HttpResponse(
                status=200,
                body=(FIXTURES / "cbr_ratings_navigation.json").read_bytes(),
            )
        return response(
            {
                "status": "success",
                "data": {"itemCount": 3, "itemList": []},
                "errors": [],
            }
        )


class MismatchSession(FakeSession):
    def post_form(
        self,
        url: str,
        *,
        headers: Any,
        fields: Any,
        timeout_seconds: float,
    ) -> HttpResponse:
        result = super().post_form(
            url,
            headers=headers,
            fields=fields,
            timeout_seconds=timeout_seconds,
        )
        if "action=searchRatingNavigation" not in url:
            return result
        payload = json.loads(result.body)
        payload["data"]["pageCount"] = 1
        return response(payload)


class NoResultsSession(FakeSession):
    def post_form(
        self,
        url: str,
        *,
        headers: Any,
        fields: Any,
        timeout_seconds: float,
    ) -> HttpResponse:
        self.posts.append((url, dict(headers), dict(fields)))
        return response(
            {
                "status": "error",
                "data": None,
                "errors": [{"code": 0, "message": "Array"}],
            }
        )


class CbrRatingsClientTests(unittest.TestCase):
    def test_uses_csrf_session_and_consumes_all_pages(self) -> None:
        session = FakeSession()
        client = CbrRatingsClient(session=session, page_size=10)

        actions = client.fetch_by_inn("1234567890")

        self.assertEqual(len(actions), 11)
        self.assertEqual(session.get_calls, 1)
        self.assertEqual(
            [post[2].get("fields[pageNumber]") for post in session.posts],
            [None, 1, 2],
        )
        self.assertTrue(all(post[1]["X-Bitrix-Csrf-Token"] == CSRF for post in session.posts))
        self.assertEqual(session.posts[1][2]["fields[sortingDirection]"], "descending")

    def test_parses_observed_official_response_contract(self) -> None:
        client = CbrRatingsClient(session=FixtureSession())

        actions = client.fetch_by_inn("9717068640")

        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[0].rating_value, "BBB+(RU)")
        self.assertEqual(actions[0].release_date.isoformat(), "2026-04-01")
        self.assertEqual(actions[2].isin, "RU000A104WS2")

    def test_stops_when_homepage_requires_captcha(self) -> None:
        client = CbrRatingsClient(session=FakeSession(homepage=f'<div id="captcha">{CSRF}</div>'))

        with self.assertRaises(CbrRatingsCaptchaRequired):
            client.fetch_by_inn("1234567890")

    def test_maps_verified_empty_search_response_to_no_results(self) -> None:
        session = NoResultsSession()
        client = CbrRatingsClient(session=session)

        self.assertEqual(client.fetch_by_isin("RU000A000000"), ())
        self.assertEqual(len(session.posts), 1)

    def test_rejects_pagination_count_mismatch(self) -> None:
        session = MismatchSession()
        client = CbrRatingsClient(session=session, page_size=25)

        with self.assertRaisesRegex(CbrRatingsContractError, "item count mismatch"):
            client.fetch_by_isin("RU000A123456")


if __name__ == "__main__":
    unittest.main()
