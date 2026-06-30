"""Background CGI JSON parsing after a user-visible stream completes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.agent.llm_provider import LLMProviderFactory
from app.agent.retry import classify_provider_exception, exponential_backoff_seconds
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager
from app.core import (
    CGIMemoryRecord,
    CGIMemoryWrite,
    CGIParseJobRecord,
    CGIParseJobWrite,
    CGIStaleJobRecoveryResult,
    LLMUsageWrite,
    SQLiteCGIMemoryStore,
    SQLiteUsageStore,
    cgi_memory_json_schema,
    estimate_text_tokens,
    estimate_usage_cost,
    parse_cgi_memory_document,
)

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True, slots=True)
class CGIParseJob:
    """Completed response passed from the streaming path to background parsing."""

    session_id: str | None
    user_message: str
    assistant_answer: str


class CGIBackgroundParser:
    """Parse streamed answers into CGI memory using a cheap JSON-focused model."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider_factory: LLMProviderFactory,
        runtime_registry: LLMRuntimeRegistry,
        memory_store: SQLiteCGIMemoryStore,
        usage_store: SQLiteUsageStore | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._provider_factory = provider_factory
        self._runtime_registry = runtime_registry
        self._memory_store = memory_store
        self._usage_store = usage_store

    async def parse_and_store(self, job: CGIParseJob) -> CGIMemoryRecord:
        """Generate CGI JSON with the configured parse model and persist it."""

        settings = self._config_manager.get_effective_settings()
        binding = self._provider_factory.create(
            provider=settings.cgi_parse_provider,
            model=settings.cgi_parse_model,
        )
        prompt = self._build_prompt(job, max_nodes=settings.cgi_parse_max_nodes)
        payload = await self._runtime_registry.generate_json(
            binding,
            prompt,
            cgi_memory_json_schema(),
        )
        payload_text = json.dumps(payload, ensure_ascii=False)
        if self._usage_store is not None:
            prompt_tokens = estimate_text_tokens(prompt)
            completion_tokens = estimate_text_tokens(payload_text)
            estimated_cost_usd = estimate_usage_cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                input_cost_per_million=settings.llm_usage_input_cost_per_million,
                output_cost_per_million=settings.llm_usage_output_cost_per_million,
            )
            await asyncio.to_thread(
                self._usage_store.save,
                LLMUsageWrite(
                    provider=binding.provider,
                    model=binding.model,
                    operation="cgi_parse",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    metadata={
                        "session_id": job.session_id,
                        "token_source": "local_estimate",
                    },
                ),
            )
            logger.info(
                "LLM usage recorded provider=%s model=%s operation=cgi_parse "
                "prompt_tokens=%s completion_tokens=%s estimated_cost_usd=%.8f",
                binding.provider,
                binding.model,
                prompt_tokens,
                completion_tokens,
                estimated_cost_usd,
            )
        document = parse_cgi_memory_document(payload).limited(settings.cgi_parse_max_nodes)
        record = await asyncio.to_thread(
            self._memory_store.save,
            CGIMemoryWrite(
                session_id=job.session_id,
                user_message=job.user_message,
                assistant_answer=job.assistant_answer,
                parser_provider=binding.provider,
                parser_model=binding.model,
                document=document,
            ),
        )
        prune_result = await asyncio.to_thread(
            self._memory_store.prune,
            max_interactions=settings.cgi_memory_max_interactions,
            min_node_weight=settings.cgi_memory_prune_min_weight,
            strategy="escape_node_pruner",
        )
        logger.info(
            "Background CGI parsing stored interaction_id=%s nodes=%s edges=%s "
            "parser_provider=%s parser_model=%s pruning_strategy=%s "
            "pruned_interactions=%s pruned_nodes=%s pruned_edges=%s",
            record.interaction_id,
            record.node_count,
            record.edge_count,
            binding.provider,
            binding.model,
            prune_result.strategy,
            prune_result.pruned_interactions,
            prune_result.pruned_nodes,
            prune_result.pruned_edges,
        )
        return record

    async def enqueue(self, job: CGIParseJob) -> CGIParseJobRecord:
        """Persist a parse job before provider work starts."""

        settings = self._config_manager.get_effective_settings()
        record = await asyncio.to_thread(
            self._memory_store.enqueue_parse_job,
            CGIParseJobWrite(
                session_id=job.session_id,
                user_message=job.user_message,
                assistant_answer=job.assistant_answer,
                max_attempts=settings.cgi_parse_job_max_attempts,
            ),
        )
        logger.info(
            "Background CGI parse job queued job_id=%s session_id=%s status=%s",
            record.id,
            record.session_id,
            record.status,
        )
        return record

    async def enqueue_and_process_safely(self, job: CGIParseJob) -> CGIParseJobRecord | None:
        """Queue a completed stream and process due jobs without affecting the response."""

        try:
            queued = await self.enqueue(job)
            settings = self._config_manager.get_effective_settings()
            await self.process_due_jobs(limit=settings.cgi_parse_job_limit_per_run)
            return queued
        except Exception:
            logger.warning(
                "Background CGI parse queue failed after stream completion",
                exc_info=True,
            )
            return None

    async def process_due_jobs(self, *, limit: int | None = None) -> list[CGIParseJobRecord]:
        """Claim and process due CGI parse jobs with smart retry handling."""

        settings = self._config_manager.get_effective_settings()
        run_limit = limit if limit is not None else settings.cgi_parse_job_limit_per_run
        processed: list[CGIParseJobRecord] = []
        for _ in range(run_limit):
            job = await asyncio.to_thread(self._memory_store.claim_next_parse_job)
            if job is None:
                break
            processed.append(await self._process_claimed_job(job))
        return processed

    async def recover_stale_jobs(self) -> CGIStaleJobRecoveryResult:
        """Recover zombie PROCESSING jobs left behind by crashed workers."""

        settings = self._config_manager.get_effective_settings()
        result = await asyncio.to_thread(
            self._memory_store.recover_stale_parse_jobs,
            stale_after_seconds=settings.cgi_parse_stale_lock_seconds,
            limit=settings.cgi_parse_recovery_batch_limit,
        )
        if result.scanned:
            logger.warning(
                "Background CGI stale-lock recovery scanned=%s requeued=%s failed=%s job_ids=%s",
                result.scanned,
                result.requeued,
                result.failed,
                ",".join(result.job_ids),
            )
        return result

    async def run_stale_recovery_loop(self) -> None:
        """Run stale lock recovery forever until the hosting task is cancelled."""

        logger.info("Background CGI stale-lock recovery loop started")
        while True:
            try:
                await self.recover_stale_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Background CGI stale-lock recovery failed", exc_info=True)

            settings = self._config_manager.get_effective_settings()
            await asyncio.sleep(settings.cgi_parse_recovery_interval_seconds)

    async def parse_and_store_safely(self, job: CGIParseJob) -> CGIMemoryRecord | None:
        """Run background parsing without breaking the completed user response."""

        try:
            return await self.parse_and_store(job)
        except Exception:
            logger.warning("Background CGI parsing failed after stream completion", exc_info=True)
            return None

    async def _process_claimed_job(self, job: CGIParseJobRecord) -> CGIParseJobRecord:
        settings = self._config_manager.get_effective_settings()
        try:
            record = await self.parse_and_store(
                CGIParseJob(
                    session_id=job.session_id,
                    user_message=job.user_message,
                    assistant_answer=job.assistant_answer,
                )
            )
        except Exception as exc:
            classification = classify_provider_exception(exc)
            if classification.kind == "quota":
                stopped = await asyncio.to_thread(
                    self._memory_store.mark_parse_job_failed,
                    job.id,
                    status="QUOTA_LOCKED",
                    error_type=classification.error_type,
                    error_message=classification.message,
                )
                logger.warning(
                    "Background CGI parse job quota-locked job_id=%s attempts=%s "
                    "error_type=%s message=%s",
                    stopped.id,
                    stopped.attempts,
                    stopped.last_error_type,
                    _safe_log_message(stopped.last_error_message),
                )
                return stopped

            if classification.kind == "transient" and job.attempts < job.max_attempts:
                delay_seconds = exponential_backoff_seconds(
                    attempt=job.attempts,
                    base_seconds=settings.cgi_parse_job_retry_base_seconds,
                    max_seconds=settings.cgi_parse_job_retry_max_seconds,
                    provider_retry_after_seconds=classification.retry_after_seconds,
                )
                next_run_at = _sqlite_timestamp_after(delay_seconds)
                rescheduled = await asyncio.to_thread(
                    self._memory_store.reschedule_parse_job,
                    job.id,
                    next_run_at=next_run_at,
                    error_type=classification.error_type,
                    error_message=classification.message,
                )
                logger.warning(
                    "Background CGI parse job rescheduled job_id=%s attempts=%s/%s "
                    "next_run_at=%s error_type=%s message=%s",
                    rescheduled.id,
                    rescheduled.attempts,
                    rescheduled.max_attempts,
                    rescheduled.next_run_at,
                    rescheduled.last_error_type,
                    _safe_log_message(rescheduled.last_error_message),
                )
                return rescheduled

            failed = await asyncio.to_thread(
                self._memory_store.mark_parse_job_failed,
                job.id,
                status="FAILED",
                error_type=classification.error_type,
                error_message=classification.message,
            )
            logger.warning(
                "Background CGI parse job failed job_id=%s attempts=%s/%s error_type=%s message=%s",
                failed.id,
                failed.attempts,
                failed.max_attempts,
                failed.last_error_type,
                _safe_log_message(failed.last_error_message),
            )
            return failed

        completed = await asyncio.to_thread(
            self._memory_store.complete_parse_job,
            job.id,
            interaction_id=record.interaction_id,
        )
        logger.info(
            "Background CGI parse job completed job_id=%s interaction_id=%s attempts=%s",
            completed.id,
            completed.interaction_id,
            completed.attempts,
        )
        return completed

    @staticmethod
    def _build_prompt(job: CGIParseJob, *, max_nodes: int) -> str:
        return (
            "Extract a compact CGI memory graph from this completed assistant response.\n"
            f"Maximum nodes: {max_nodes}.\n"
            "Create nodes only for durable facts, decisions, preferences, project state, "
            "or meaningful relationships. Avoid transient filler and duplicate labels.\n\n"
            f"User message:\n{job.user_message}\n\n"
            f"Assistant answer:\n{job.assistant_answer}"
        )


def _sqlite_timestamp_after(delay_seconds: float) -> str:
    timestamp = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _safe_log_message(message: str | None, *, limit: int = 500) -> str:
    if not message:
        return ""
    normalized = " ".join(message.split())
    return normalized[:limit]
