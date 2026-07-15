# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""
Async batch processing for LLM API requests.

Provides Batch for concurrent execution of multiple LLM API calls
with dynamic request queuing and result streaming via async iterator.
Routes requests to providers based on context via the unified async generate API.

Example:
    batch = Batch(max_concurrent=5)

    req = batch.create(contents="What is 2+2?", context="myapp.calc")
    req.my_id = "calc1"
    batch.add(req)

    async for req in batch.drain_batch():
        print(f"{req.my_id}: {req.response}")

"""

import asyncio
import time
from typing import Any, List, Optional, Union

from solstone.think.models import (
    SchemaValidationError,
    agenerate_with_result,
    finish_reason_error,
    resolve_provider,
)
from solstone.think.providers.shared import classify_provider_error


class BatchRequest:
    """
    Mutable request object for a single LLM API call.

    Core attributes are passed to agenerate_with_result(). Callers can add
    arbitrary attributes for tracking (e.g., frame_id, stage, etc).

    After execution, these attributes are populated:
        - response: Optional[str] - Response text (None if error)
        - error: Optional[str] - Error message (None if success)
        - duration: float - Execution time in seconds
        - model_used: str - Model that was used
    """

    def __init__(
        self,
        contents: Union[str, List[Any]],
        context: str,
        temperature: float = 0.3,
        max_output_tokens: int = 8192 * 2,
        system_instruction: Optional[str] = None,
        json_output: bool = False,
        json_schema: Optional[dict] = None,
        thinking_budget: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ):
        self.contents = contents
        self.context = context
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.system_instruction = system_instruction
        self.json_output = json_output
        self.json_schema = json_schema
        self.thinking_budget = thinking_budget
        self.timeout_s = timeout_s

        # Populated after execution
        self.response: Optional[str] = None
        self.error: Optional[str] = None
        self.duration: float = 0.0
        self.model_used: str = ""
        self.reason_code: Optional[str] = None
        self.provider: Optional[str] = None
        self.reset_at_ms: Optional[int] = None


class Batch:
    """
    Async batch processor for LLM API requests.

    Manages concurrent execution with dynamic request queuing and result
    streaming via async iterator pattern. Routes to providers via async generation.

    Example:
        batch = Batch(max_concurrent=5)

        # Add requests
        req1 = batch.create(contents="What is 2+2?", context="myapp.calc")
        req1.task_id = "calc1"
        batch.add(req1)

        req2 = batch.create(contents="What is 3+3?", context="myapp.calc")
        req2.task_id = "calc2"
        batch.add(req2)

        # Process results as they complete
        async for req in batch.drain_batch():
            print(f"{req.task_id}: {req.response}")
    """

    def __init__(self, max_concurrent: int = 5):
        """
        Initialize batch processor.

        Parameters
        ----------
        max_concurrent : int
            Maximum number of concurrent API requests (default: 5)
        """
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.result_queue: asyncio.Queue = asyncio.Queue()
        self.pending_tasks: set = set()

    def create(
        self,
        contents: Union[str, List[Any]],
        context: str,
        temperature: float = 0.3,
        max_output_tokens: int = 8192 * 2,
        system_instruction: Optional[str] = None,
        json_output: bool = False,
        json_schema: Optional[dict] = None,
        thinking_budget: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> BatchRequest:
        """
        Create a new BatchRequest.

        Convenience factory method. Caller can add arbitrary attributes
        to the returned request before calling add().

        Parameters
        ----------
        contents : str or List
            The content to send to the model
        context : str
            Context string for provider routing (e.g., "observe.describe.frame")
        Returns
        -------
        BatchRequest
            New request object ready to be customized and added
        """
        return BatchRequest(
            contents=contents,
            context=context,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            json_output=json_output,
            json_schema=json_schema,
            thinking_budget=thinking_budget,
            timeout_s=timeout_s,
        )

    def add(self, request: BatchRequest) -> None:
        """
        Add request to batch for execution.

        Request will be executed concurrently (up to max_concurrent limit).
        Non-blocking - returns immediately. Can be called at any time, even
        during iteration or after draining.

        Parameters
        ----------
        request : BatchRequest
            Request to execute
        """
        task = asyncio.create_task(self._execute_request(request))
        self.pending_tasks.add(task)

    def update(self, request: BatchRequest, **kwargs) -> None:
        """
        Update request attributes and re-add to batch for execution.

        This is useful for retries or multi-stage processing where you want
        to reuse the same request object with different parameters.

        Parameters
        ----------
        request : BatchRequest
            Request to update and re-execute
        **kwargs
            Any attributes to update on the request object

        Example
        -------
        >>> batch.update(
        ...     req,
        ...     contents="New prompt",
        ...     temperature=0.8,
        ...     custom_attr="foo"
        ... )
        """
        # Update any provided attributes
        for key, value in kwargs.items():
            setattr(request, key, value)

        # Clear previous execution results
        request.response = None
        request.error = None
        request.duration = 0.0
        request.reason_code = None
        request.provider = None
        request.reset_at_ms = None

        # Re-add to batch
        self.add(request)

    def is_drained(self) -> bool:
        """
        Check if batch is fully drained.

        Returns True when there are no pending tasks and no results waiting
        in the queue.

        Returns
        -------
        bool
            True if batch is drained, False otherwise
        """
        # Clean up completed tasks
        self.pending_tasks = {t for t in self.pending_tasks if not t.done()}
        return len(self.pending_tasks) == 0 and self.result_queue.empty()

    async def wait_until_drained(self) -> None:
        """
        Wait until all pending work completes and queue is empty.

        Blocks until is_drained() returns True.
        """
        while not self.is_drained():
            await asyncio.sleep(0.1)

    async def _execute_request(self, request: BatchRequest) -> None:
        """
        Execute a single request and put result in queue.

        Parameters
        ----------
        request : BatchRequest
            Request to execute (will be modified in place)
        """
        start_time = time.time()
        try:
            async with self.semaphore:
                result = await agenerate_with_result(
                    contents=request.contents,
                    context=request.context,
                    temperature=request.temperature,
                    max_output_tokens=request.max_output_tokens,
                    system_instruction=request.system_instruction,
                    json_output=request.json_output,
                    json_schema=request.json_schema,
                    thinking_budget=request.thinking_budget,
                    timeout_s=request.timeout_s,
                )
                error = finish_reason_error(
                    result,
                    json_output=request.json_output,
                )
                if error is not None:
                    raise error
                validation = result.get("schema_validation")
                if isinstance(validation, dict) and validation.get("valid") is False:
                    raise SchemaValidationError(
                        validation.get("errors") or [],
                        result.get("text", ""),
                    )
                request.duration = time.time() - start_time
                request.response = result["text"]
                request.error = None

                request.model_used = str(result.get("model") or "")
        except Exception as e:
            request.duration = time.time() - start_time
            request.response = None
            request.error = str(e)
            request.reason_code = getattr(e, "reason_code", None) or (
                classify_provider_error(e, request.context or "")
            )
            request.reset_at_ms = getattr(e, "reset_at_ms", None)
            request.provider = getattr(e, "provider", None)
            if request.provider is None:
                try:
                    request.provider = resolve_provider("generate")[0]
                except (KeyError, TypeError, ValueError):
                    request.provider = None

        # Put completed request in result queue
        await self.result_queue.put(request)

    async def drain_batch(self):
        """
        Async generator that yields completed requests until batch is drained.

        Yields results from the queue while there's still pending work OR
        results waiting. Stops when both pending_tasks is empty AND queue
        is empty.

        This can be called multiple times - each call will drain whatever
        work is currently in the batch.

        Yields
        ------
        BatchRequest
            Completed request with response/error populated

        Example
        -------
        >>> async for req in batch.drain_batch():
        ...     print(req.response)
        ...     if req.error:
        ...         batch.add(req)  # Retry on error
        """
        while True:
            # Check if we're truly drained
            self.pending_tasks = {t for t in self.pending_tasks if not t.done()}

            # If drained, stop iteration
            if len(self.pending_tasks) == 0 and self.result_queue.empty():
                break

            # Try to get a result (with timeout to avoid blocking forever)
            try:
                result = await asyncio.wait_for(self.result_queue.get(), timeout=0.1)
                yield result
            except asyncio.TimeoutError:
                # No result ready yet, but might have pending work
                continue


__all__ = ["BatchRequest", "Batch"]
