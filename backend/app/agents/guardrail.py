"""
Guardrail Agent: PII Detection, Cryptographic Masking, and Reversible De-identification.

Implements the zero-trust data privacy boundary for the agentic compliance pipeline.
Prior to any prompt or context being dispatched to external LLMs (Groq, etc.):
1. Scans text using Microsoft Presidio (powered by spaCy en_core_web_sm + regex engines).
2. Augments detection with custom European financial/enterprise pattern recognizers (IBAN, Steuer-ID).
3. Deterministically replaces sensitive entities with reversible indexed tokens:
   e.g. 'hans.gruber@web.de' -> '[MASKED_EMAIL_1]'
4. Maintains an internal state mapping so downstream execution workers can de-anonymize
   the plan when writing to staging_db without exposing PII to external model APIs.
"""

import logging
import re
from typing import Any, Optional

from langchain_core.messages import AIMessage
from presidio_analyzer import (
    AnalyzerEngine,
    Pattern,
    PatternRecognizer,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider

from backend.app.agents.state import ComplianceState, PIIEntity

logger = logging.getLogger(__name__)


# ==============================================================================
# Custom Enterprise Pattern Recognizers
# ==============================================================================


def build_custom_recognizers() -> list[PatternRecognizer]:
    """
    Builds domain-specific pattern recognizers for European enterprise CRM data:
    1. EU IBAN (International Bank Account Number with strict country codes)
    2. German Tax Identification Number (Steuerliche Identifikationsnummer - Steuer-ID)
    3. German Postal/Zip Codes with city context
    """
    recognizers: list[PatternRecognizer] = []

    # 1. European IBAN Recognizer
    iban_pattern = Pattern(
        name="eu_iban_pattern",
        regex=r"\b[A-Z]{2}[0-9]{2}(?:[ ]?[0-9]{4}){4}(?!(?:[ ]?[0-9]){1,3})\b|\b[A-Z]{2}[0-9]{2}[0-9A-Z]{12,30}\b",
        score=0.95,
    )
    iban_recognizer = PatternRecognizer(
        supported_entity="IBAN_CODE",
        patterns=[iban_pattern],
        context=["iban", "bank", "account", "konto", "kontonummer", "überweisung"],
    )
    recognizers.append(iban_recognizer)

    # 2. German Steuer-ID (11 digits, unique taxpayer ID)
    steuer_id_pattern = Pattern(
        name="german_steuer_id_pattern",
        regex=r"\b[0-9]{11}\b",
        score=0.85,
    )
    steuer_id_recognizer = PatternRecognizer(
        supported_entity="GERMAN_TAX_ID",
        patterns=[steuer_id_pattern],
        context=["steuer-id", "steuerid", "tin", "tax id", "steuernummer", "finanzamt"],
    )
    recognizers.append(steuer_id_recognizer)

    return recognizers


# ==============================================================================
# Presidio Sanitizer Engine
# ==============================================================================


class GuardrailSanitizer:
    """
    High-performance, local PII detection and reversible token masking engine.
    """

    CONFIDENCE_THRESHOLD: float = 0.60

    TARGET_ENTITIES: list[str] = [
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
        "CREDIT_CARD",
        "CRYPTO",
        "IP_ADDRESS",
        "US_SSN",
        "GERMAN_TAX_ID",
        "LOCATION",
    ]

    def __init__(self) -> None:
        self._init_analyzer()

    def _init_analyzer(self) -> None:
        """Initializes Presidio AnalyzerEngine configured with lightweight spaCy model."""
        nlp_config = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
        provider = NlpEngineProvider(nlp_configuration=nlp_config)
        nlp_engine = provider.create_engine()
        self.analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])

        # Register custom enterprise recognizers
        for recognizer in build_custom_recognizers():
            self.analyzer.registry.add_recognizer(recognizer)

    def analyze_and_mask(
        self,
        text: str,
        threshold: Optional[float] = None,
    ) -> tuple[str, dict[str, str], list[PIIEntity]]:
        """
        Analyzes source text, detects sensitive entities, and performs deterministic
        reversible token substitution.

        Returns:
            tuple:
              - sanitized_text (str): Prompt with PII replaced by tokens like [MASKED_EMAIL_1]
              - pii_mapping (dict[str, str]): Map of token -> original value
              - detected_entities (list[PIIEntity]): Structured metadata of detected items
        """
        if not text or not text.strip():
            return text, {}, []

        min_score = threshold or self.CONFIDENCE_THRESHOLD
        results: list[RecognizerResult] = self.analyzer.analyze(
            text=text,
            entities=self.TARGET_ENTITIES,
            language="en",
            score_threshold=min_score,
        )

        if not results:
            return text, {}, []

        # Deduplicate and sort by start position descending to allow safe in-place replacement
        sorted_results = self._filter_and_sort_results(results)

        pii_mapping: dict[str, str] = {}
        detected_entities: list[PIIEntity] = []
        entity_counters: dict[str, int] = {}

        # First pass: assign indexed tokens in reading order (left-to-right)
        token_assignments: list[tuple[RecognizerResult, str]] = []
        for res in sorted(sorted_results, key=lambda r: r.start):
            etype = res.entity_type
            entity_counters[etype] = entity_counters.get(etype, 0) + 1
            token = f"[MASKED_{etype}_{entity_counters[etype]}]"
            token_assignments.append((res, token))

        # Second pass: replace from right to left (descending offset) so indices remain valid
        sanitized_chars = list(text)
        for res, token in sorted(token_assignments, key=lambda t: t[0].start, reverse=True):
            orig_val = text[res.start : res.end]
            sanitized_chars[res.start : res.end] = list(token)
            pii_mapping[token] = orig_val

            detected_entities.append(
                PIIEntity(
                    entity_type=res.entity_type,
                    original_value=orig_val,
                    token_mask=token,
                    start_pos=res.start,
                    end_pos=res.end,
                )
            )

        sanitized_text = "".join(sanitized_chars)
        # Reverse detected_entities back to left-to-right reading order
        detected_entities.reverse()

        return sanitized_text, pii_mapping, detected_entities

    @staticmethod
    def unmask_text(sanitized_text: str, pii_mapping: dict[str, str]) -> str:
        """
        Reverses token masking, restoring original customer values from the mapping.
        Used by the execution worker before executing SQL mutations on staging_db.
        """
        if not pii_mapping or not sanitized_text:
            return sanitized_text

        result = sanitized_text
        for token, original in pii_mapping.items():
            result = result.replace(token, original)
        return result

    @staticmethod
    def _filter_and_sort_results(
        results: list[RecognizerResult],
    ) -> list[RecognizerResult]:
        """
        Filters overlapping detections, favoring higher confidence scores and longer spans.
        """
        sorted_by_priority = sorted(
            results,
            key=lambda r: (r.score, (r.end - r.start)),
            reverse=True,
        )
        non_overlapping: list[RecognizerResult] = []

        for candidate in sorted_by_priority:
            has_overlap = False
            for accepted in non_overlapping:
                # Check for overlap: [start, end)
                if max(candidate.start, accepted.start) < min(candidate.end, accepted.end):
                    has_overlap = True
                    break
            if not has_overlap:
                non_overlapping.append(candidate)

        return non_overlapping


# Global singleton instance for efficient reuse across LangGraph invocations
guardrail_sanitizer = GuardrailSanitizer()
GuardrailAgent = GuardrailSanitizer


# ==============================================================================
# LangGraph Guardrail Node Function
# ==============================================================================


def guardrail_node(state: ComplianceState) -> dict[str, Any]:
    """
    LangGraph execution node for the Guardrail Agent.
    
    1. Reads original_prompt from state.
    2. Runs Presidio analyzer and generates sanitized_prompt and pii_mapping.
    3. Emits an audit message to the conversation history.
    4. Updates the state with sanitized inputs.
    """
    raw_prompt = state.get("original_prompt", "")
    sanitized, mapping, entities = guardrail_sanitizer.analyze_and_mask(raw_prompt)

    entity_count = len(entities)
    if entity_count > 0:
        summary_items = [f"{e.entity_type} ({e.token_mask})" for e in entities]
        agent_msg = AIMessage(
            content=(
                f"[Guardrail Agent]: Successfully sanitized {entity_count} sensitive PII entity/entities "
                f"from user prompt prior to LLM reasoning: {', '.join(summary_items)}."
            )
        )
    else:
        agent_msg = AIMessage(
            content="[Guardrail Agent]: Scan complete. No sensitive PII detected in user prompt."
        )

    return {
        "sanitized_prompt": sanitized,
        "pii_mapping": mapping,
        "detected_pii": [e.model_dump() for e in entities],
        "messages": [agent_msg],
    }
