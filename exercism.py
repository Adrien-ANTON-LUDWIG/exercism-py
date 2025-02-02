#!/usr/bin/python3
"""Exercism API module."""

# Standard lib
import asyncio
import datetime
import itertools
import json
import logging
import os
import pathlib
import time
from typing import Any, Callable, Iterable

# External libs
import aiohttp  # type: ignore
import requests  # type: ignore
import tenacity  # type: ignore


Notifications = dict[str, Any]


class Exercism:
    """Exercism API wrapper."""

    # exercism.org API endpoint.
    API = "https://exercism.org/api/v2"
    # Sleep 15 minutes between watch polling.
    WATCHER_SLEEP_SEC = 15 * 60

    def __init__(self):
        """Iniitialize the wrapper."""
        # Get the user token from the exercism cli config file.
        config = pathlib.Path(os.getenv("XDG_CONFIG_HOME")) / "exercism" / "user.json"
        token = json.loads(config.read_text())["token"]
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def request(self, func, *args, sleep: float = 0.5, **kwargs) -> requests.Response:
        """Perform an HTTP request with 429 handling."""
        resp = func(*args, **kwargs)
        if "retry-after" in resp.headers:
            delay = int(resp.headers["retry-after"])
            logging.info("Rate limited. Sleep %d and retry.", delay)
            time.sleep(delay + 1)
            resp = func(*args, **kwargs)

        resp.raise_for_status()
        time.sleep(sleep)
        return resp

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=2, min=15, max=60),
        retry=tenacity.retry_if_exception_type((
            requests.HTTPError, requests.exceptions.ConnectionError
        )),
    )
    def get_json_with_retries(self, *args, sleep: float = 0.2, **kwargs) -> dict[str, Any]:
        """Return JSON returns from an HTTP GET. Retry on failure."""
        resp = self.request(self.session.get, *args, sleep=sleep, **kwargs)
        return resp.json()

    def get_all_pages(self, *args, endpoint: str, **kwargs) -> list[dict[str, Any]]:
        """Return all pages from a paginated result."""
        params = kwargs.pop("params", {})
        all_data = []
        url = f"{self.API}/{endpoint}"
        for page in itertools.count(start=1):
            params["page"] = str(page)
            response = self.get_json_with_retries(url, *args, params=params, **kwargs)
            all_data.extend(response["results"])
            if response["meta"]["current_page"] >= response["meta"]["total_pages"]:
                break
        return all_data

    def post(self, *args, sleep: float = 0.5, **kwargs) -> requests.Response:
        """HTTP POST."""
        return self.request(self.session.post, *args, sleep=sleep, **kwargs)

    def patch(self, *args, sleep: float = 0.5, **kwargs) -> requests.Response:
        """HTTP PATCH."""
        return self.request(self.session.patch, *args, sleep=sleep, **kwargs)

    def notifications(self) -> Notifications:
        """Return notifications."""
        return self.get_json_with_retries(f"{self.API}/notifications")

    def print_unread_notifications(self):
        """Pretty print unread notifications."""
        notifications = self.notifications()
        print(f"Unread count: {notifications['meta']['unread_count']}")
        for result in notifications["results"]:
            if result["is_read"]:
                continue
            print(f"{result['url']} {result['text']}")

    def print_nonpassing_solutions(self) -> None:
        """Print a list of exercises which are not passing."""
        solutions = self.get_all_pages(endpoint="solutions")
        for solution in solutions:
            if solution["published_iteration_head_tests_status"] == "passed":
                continue
            print(
                f"{solution['published_iteration_head_tests_status'].upper()}: "
                f"Updated {solution['track']['slug']}/{solution['exercise']['slug']}"
            )

    def update_exercises(self) -> list[dict]:
        """Refresh the exercises on a track."""
        updates = []
        solutions = self.get_all_pages(endpoint="solutions")
        for solution in solutions:
            if not solution["is_out_of_date"]:
                continue
            uuid = solution["uuid"]
            resp = self.patch(f"{self.API}/solutions/{uuid}/sync")
            updates.append(resp.json()["solution"])

        return updates

    def update_exercises_and_print(self) -> None:
        """Update stale exerises and print new states."""
        updates = self.update_exercises()
        states = {update["published_iteration_head_tests_status"] for update in updates}
        for state in sorted(states):
            for update in updates:
                if update["published_iteration_head_tests_status"] == state:
                    print(
                        f"{state.upper()}: Updated "
                        f"{update['track']['slug']}/{update['exercise']['slug']}"
                    )

    def notification_pusher(self, callback: Callable[[dict[str, str]], None]) -> None:
        """Watch for new notifications and call `callback` with them."""
        # Ignore existing notifications.
        seen_notifications = set(r["uuid"] for r in self.notifications()["results"])
        while True:
            time.sleep(self.WATCHER_SLEEP_SEC)
            try:
                unread = [r for r in self.notifications()["results"] if not r["is_read"]]
            except requests.exceptions.HTTPError:
                continue
            unseen = [r for r in unread if r["uuid"] not in seen_notifications]
            for result in unseen:
                callback(result)
            seen_notifications.update(r["uuid"] for r in unseen)

    def streaming_events(self, live: bool) -> list[dict[str, Any]]:
        """Return all streaming_events."""
        params = {}
        if live:
            params["live"] = True
        all_data = self.get_all_pages(endpoint="streaming_events", params=params)
        for i in all_data:
            for key in ["starts_at", "ends_at"]:
                parsed = datetime.datetime.strptime(i[key], "%Y-%m-%dT%H:%M:%S.000Z")
                i[key] = parsed.replace(tzinfo=datetime.timezone.utc)
        all_data.sort(key=lambda x: x["starts_at"])
        return all_data

    def future_streaming_events(self) -> list[dict[str, Any]]:
        """Return streaming_events which are in the future."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return [
            i for i in self.streaming_events(False)
            if i["starts_at"] >= now
        ]

    def mentor_requests(self, track: str) -> list[dict[str, Any]]:
        """Return all mentoring requests for one track."""
        params = {"track_slug": track.lower()}
        return self.get_all_pages(endpoint="mentoring/requests", params=params, sleep=0.5)

    def all_tracks(self) -> list[str]:
        """Return all the tracks."""
        return [i["slug"] for i in self.get_json_with_retries(f"{self.API}/tracks")["tracks"]]

    def mentor_discussion_posts(self, uuid: str) -> list[dict[str, Any]]:
        """Return mentor discussion posts for one discussion."""
        return self.get_json_with_retries(f"{self.API}/mentoring/discussions/{uuid}/posts")["items"]

    def old_mentor_discussions(self, status: str, age: int, order: str = "oldest") -> list[str]:
        # pylint: disable=R0914
        """Get mentor discussions more than a certain age (days)."""
        assert order in ("oldest", "recent", "exercise", "student")
        assert status in ("awaiting_mentor", "awaiting_student", "finished")

        params: dict[str, str | int] = {"status": status, "order": order}
        resp = self.get_json_with_retries(f"{self.API}/mentoring/discussions", params=params)
        page_count = resp["meta"]["total_pages"]

        delta = datetime.timedelta(days=age)
        cutoff = datetime.datetime.now() - delta
        uuids = []
        for page in range(1, page_count + 1):
            logging.info("Fetching old discussions, page %d of %d", page, page_count)
            params["page"] = page
            resp = self.get_json_with_retries(f"{self.API}/mentoring/discussions", params=params)
            for discussion in resp["results"]:
                updated = datetime.datetime.strptime(discussion["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
                if updated > cutoff:
                    continue

                uuid = discussion["uuid"]
                posts = self.get_json_with_retries(
                    f"{self.API}/mentoring/discussions/{uuid}/posts"
                )["items"]
                post_dates = [
                    datetime.datetime.strptime(c['updated_at'], "%Y-%m-%dT%H:%M:%SZ") for c in posts
                ]
                most_recent_post = max(post_dates)
                if most_recent_post > cutoff:
                    continue

                uuids.append(uuid)
        return uuids

    def finish(self, uuids: Iterable[str]) -> None:
        """Finish student discussions."""
        for uuid in uuids:
            self.request(
                self.session.patch,
                f"{self.API}/mentoring/discussions/{uuid}/finish",
                sleep=0.2,
            )

    def nudge(self, uuids: Iterable[str], msg: str = "") -> None:
        """Nudge student discussions."""
        if not msg:
            msg = (
                "It's been a while! "
                "How is this exercise going? Are you planning on making more changes? "
                "If you are ready to move on, you can free up the mentoring slot by clicking "
                '"End discussion". '
                "If you are still working on this, that is great, too! "
                "If you have any questions or what any help or tips, just let me know!"
            )
        for uuid in uuids:
            self.post(f"{self.API}/mentoring/discussions/{uuid}/posts", data={"content": msg})

    def failing_solutions(self, track: None | str = None):
        """Get solutions which are not passing."""
        params: dict[str, str | int] = {}
        resp = self.get_json_with_retries(f"{self.API}/solutions", params=params)
        page_count = resp["meta"]["total_pages"]

        solutions = []
        for page in range(1, page_count + 1):
            logging.info("Fetching failing solutions, page %d of %d", page, page_count)
            params["page"] = page
            resp = self.get_json_with_retries(f"{self.API}/solutions", params=params)
            for solution in resp["results"]:
                if track and solution["track"]["slug"] != track:
                    continue
                if solution["published_iteration_head_tests_status"] in ("passed", "not_queued"):
                    continue
                keys = (
                    "uuid", "private_url", "published_iteration_head_tests_status",
                    "published_at", "completed_at", "updated_at", "is_out_of_date",
                )
                solutions.append({f: solution[f] for f in keys})
                for key in ("exercise", "track"):
                    solutions[-1][key] = solution[key]["slug"]
        return solutions


class AsyncExercism:
    """Exercism API wrapper."""

    API = "https://exercism.org/api/v2"

    def __init__(self):
        """Iniitialize the wrapper."""
        # Get the user token from the exercism cli config file.
        config = pathlib.Path(os.getenv("XDG_CONFIG_HOME")) / "exercism" / "user.json"
        token = json.loads(config.read_text())["token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=2, min=15, max=60),
        retry=tenacity.retry_if_exception_type((
            requests.HTTPError, requests.exceptions.ConnectionError
        )),
        sleep=asyncio.sleep,  # type: ignore
    )
    async def get_json_with_retries(
        self,
        *args,
        endpoint: str,
        session: aiohttp.ClientSession,
        sleep: float = 0.2,
        **kwargs
    ) -> dict[str, Any]:
        """Return JSON returns from an HTTP GET. Retry on failure."""
        if "headers" not in kwargs:
            kwargs["headers"] = self.headers
        async with session.get(f"{self.API}/{endpoint}", *args, **kwargs) as resp:
            if "retry-after" not in resp.headers:
                resp.raise_for_status()
                await asyncio.sleep(sleep)
                return await resp.json()

            delay = int(resp.headers["retry-after"])
        logging.info("Rate limited. Sleep %d and retry.", delay)
        await asyncio.sleep(delay + 1)
        async with session.get(*args, **kwargs) as resp:
            resp.raise_for_status()
            await asyncio.sleep(sleep)
            return await resp.json()

    async def get_all_pages(self, *args, endpoint: str, **kwargs) -> list[dict[str, Any]]:
        """Return all pages from a paginated result."""
        params = kwargs.pop("params", {})
        all_data = []
        async with aiohttp.ClientSession() as session:
            for page in itertools.count(start=1):
                params["page"] = str(page)
                response = await self.get_json_with_retries(
                    *args, params=params, endpoint=endpoint, session=session, **kwargs
                )
                all_data.extend(response["results"])
                if response["meta"]["current_page"] >= response["meta"]["total_pages"]:
                    break
        return all_data

    async def streaming_events(self, live: bool) -> list[dict[str, Any]]:
        """Return all streaming_events."""
        params = {}
        if live:
            params["live"] = True
        all_data = await self.get_all_pages(endpoint="streaming_events", params=params)
        for i in all_data:
            for key in ["starts_at", "ends_at"]:
                parsed = datetime.datetime.strptime(i[key], "%Y-%m-%dT%H:%M:%S.000Z")
                i[key] = parsed.replace(tzinfo=datetime.timezone.utc)
        all_data.sort(key=lambda x: x["starts_at"])
        return all_data

    async def future_streaming_events(self) -> list[dict[str, Any]]:
        """Return streaming_events which are in the future."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return [
            i for i in await self.streaming_events(False)
            if i["starts_at"] >= now
        ]

    async def mentor_requests(self, track: str) -> list[dict[str, Any]]:
        """Return all mentoring requests for one track."""
        params = {"track_slug": track.lower()}
        return await self.get_all_pages(endpoint="mentoring/requests", params=params, sleep=0.5)

    async def all_tracks(self) -> list[str]:
        """Return all the tracks."""
        async with aiohttp.ClientSession() as session:
            data = await self.get_json_with_retries(session=session, endpoint="tracks")
        return [i["slug"] for i in data["tracks"]]


def nudge():
    """Nudge or close out old mentor discussions."""
    exercism = Exercism()
    now = datetime.datetime.now()
    ids = exercism.old_mentor_discussions("awaiting_student", 30)
    to_finish = []
    to_nudge = []
    for uuid in ids:
        posts = exercism.get_json_with_retries(
            f"{exercism.API}/mentoring/discussions/{uuid}/posts"
        )["items"]
        first_mentor = min(
            p["updated_at"] for p in posts if not p['by_student']
        )
        last_student = max(
            (p["updated_at"] for p in posts if p['by_student']), default=first_mentor
        )
        updated = max(first_mentor, last_student)
        posts_since_updated = len([p for p in posts if p["updated_at"] >= updated])
        age = now - datetime.datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
        if age.days > 300 and posts_since_updated > 5:
            to_finish.append(uuid)
        else:
            to_nudge.append(uuid)
    print(f"Conversations to finish: {len(to_finish)}")
    print(f"Conversations to nudge: {len(to_nudge)}")
    exercism.finish(to_finish)
    exercism.nudge(to_nudge)


if __name__ == "__main__":
    nudge()
    # Exercism().print_unread_notifications()
    # for exercise in Exercism().failing_solutions("python"):
    #     print(f"https://exercism.org/tracks/{exercise['track']}/exercises/{exercise['exercise']}")


# vim:ts=4:sw=4:expandtab
