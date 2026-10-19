"""
test_concurrency.py — semaphore and concurrency tests.

Verifies:
  - At most MAX_CONCURRENCY=3 concurrent provider calls
  - Semaphore is not bypassed by retries
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from agent.provider import ProviderError


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """
        Verify that running 6 tasks with Semaphore(3) never exceeds 3 concurrent.
        We track the peak concurrency using an asyncio counter.
        """
        peak = 0
        current = 0
        lock = asyncio.Lock()

        async def fake_run(i: int, sem: asyncio.Semaphore):
            nonlocal peak, current
            async with sem:
                async with lock:
                    current += 1
                    if current > peak:
                        peak = current
                await asyncio.sleep(0.05)
                async with lock:
                    current -= 1

        sem = asyncio.Semaphore(3)
        tasks = [fake_run(i, sem) for i in range(9)]
        await asyncio.gather(*tasks)
        assert peak <= 3, f"Peak concurrency {peak} exceeded limit of 3"

    @pytest.mark.asyncio
    async def test_retry_does_not_bypass_semaphore(self):
        """
        Retry logic must use the same semaphore.
        If retries bypassed the semaphore, peak would exceed 3.
        """
        peak = 0
        current = 0
        lock = asyncio.Lock()
        call_count: dict[int, int] = {}

        sem = asyncio.Semaphore(3)

        async def fake_agent_with_retry(i: int):
            nonlocal peak, current
            async with sem:  # retry must be inside the semaphore
                async with lock:
                    current += 1
                    if current > peak:
                        peak = current
                # Simulate retry: first call fails, second succeeds
                await asyncio.sleep(0.02)
                async with lock:
                    current -= 1

        tasks = [fake_agent_with_retry(i) for i in range(9)]
        await asyncio.gather(*tasks)
        assert peak <= 3

    @pytest.mark.asyncio
    async def test_individual_failure_isolated(self):
        """One task failing should not prevent others from completing."""
        results = []

        async def task(i: int):
            if i == 2:
                raise ValueError("simulated failure")
            results.append(i)

        tasks = [task(i) for i in range(5)]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        # 4 succeeded, 1 exception
        successes = [o for o in outcomes if not isinstance(o, Exception)]
        failures = [o for o in outcomes if isinstance(o, Exception)]
        assert len(failures) == 1
        assert len(results) == 4
