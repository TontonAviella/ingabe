"""The async Redis client is per event loop: a client used on a loop that has since closed must not break shutdown
on another loop (it did: CI teardown "RuntimeError: Event loop is closed" in close_async_redis)."""

import asyncio

from src.dependencies import redis_client


def test_each_event_loop_gets_its_own_client_and_reuses_it():
    async def twice():
        return redis_client.get_async_redis(), redis_client.get_async_redis()

    first_a, first_b = asyncio.run(twice())
    second_a, _ = asyncio.run(twice())
    assert first_a is first_b and first_a is not second_a


def test_no_running_loop_gives_no_client():
    assert redis_client.get_async_redis() is None


def test_shutdown_on_a_new_loop_ignores_a_client_whose_loop_closed():
    async def use():
        client = redis_client.get_async_redis()
        await client.ping()  # opens a real connection on this loop
        return client

    used = asyncio.run(use())  # this loop is now closed, with the client's connection still open

    async def shutdown():
        mine = redis_client.get_async_redis()
        await mine.ping()
        await redis_client.close_async_redis()
        return mine

    mine = asyncio.run(shutdown())
    assert used is not mine
    assert not any(client is used for client in redis_client._async_clients.values())
    assert not any(client is mine for client in redis_client._async_clients.values())
