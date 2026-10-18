"""
Unit tests for Step 12: Guardrail Agent (backend/app/agents/guardrail.py).
"""

import pytest
from backend.app.agents.guardrail import GuardrailSanitizer, guardrail_node
from backend.app.agents.state import create_initial_state


@pytest.fixture
def sanitizer() -> GuardrailSanitizer:
    return GuardrailSanitizer()


def test_masking_standard_pii(sanitizer: GuardrailSanitizer):
    """Test detection and masking of email, person name, and phone number."""
    prompt = (
        "Please update records for client Hans Gruber with email "
        "hans.gruber@nakatomi.de and mobile phone +49 171 9876543."
    )
    sanitized, mapping, entities = sanitizer.analyze_and_mask(prompt)

    assert "hans.gruber@nakatomi.de" not in sanitized
    assert "Hans Gruber" not in sanitized
    assert "+49 171 9876543" not in sanitized

    # Verify token masks exist in the sanitized output
    assert "[MASKED_EMAIL_ADDRESS_1]" in sanitized
    assert "[MASKED_PERSON_1]" in sanitized

    # Verify reversible mapping
    assert mapping["[MASKED_EMAIL_ADDRESS_1]"] == "hans.gruber@nakatomi.de"
    assert mapping["[MASKED_PERSON_1]"] == "Hans Gruber"

    # Verify reversible unmasking restores original prompt
    restored = sanitizer.unmask_text(sanitized, mapping)
    assert restored == prompt


def test_masking_european_financial_identifiers(sanitizer: GuardrailSanitizer):
    """Test custom pattern recognizers for European IBAN and German Tax ID."""
    prompt = (
        "Transfer billing deposit to IBAN DE89370400440532013000 "
        "for client tax record with Steuer-ID 12345678901."
    )
    sanitized, mapping, entities = sanitizer.analyze_and_mask(prompt)

    assert "DE89370400440532013000" not in sanitized
    assert "12345678901" not in sanitized

    # Check IBAN or custom entities masked
    assert any("IBAN" in k for k in mapping.keys())
    assert any("TAX_ID" in k or "GERMAN" in k for k in mapping.keys())

    # Verify 100% reversible unmasking
    restored = sanitizer.unmask_text(sanitized, mapping)
    assert restored == prompt


def test_clean_prompt_no_pii(sanitizer: GuardrailSanitizer):
    """Test that generic prompts without PII remain untouched."""
    prompt = "Standardize all pending invoice amount currencies to EUR and check dates."
    sanitized, mapping, entities = sanitizer.analyze_and_mask(prompt)

    assert sanitized == prompt
    assert len(mapping) == 0
    assert len(entities) == 0


def test_multiple_entities_of_same_type(sanitizer: GuardrailSanitizer):
    """Test sequential indexing when multiple entities of the same type appear."""
    prompt = "Email alice@nakatomi.de and CC bob.vance@refrigeration.com immediately."
    sanitized, mapping, entities = sanitizer.analyze_and_mask(prompt)

    assert "[MASKED_EMAIL_ADDRESS_1]" in sanitized
    assert "[MASKED_EMAIL_ADDRESS_2]" in sanitized
    assert mapping["[MASKED_EMAIL_ADDRESS_1]"] == "alice@nakatomi.de"
    assert mapping["[MASKED_EMAIL_ADDRESS_2]"] == "bob.vance@refrigeration.com"

    restored = sanitizer.unmask_text(sanitized, mapping)
    assert restored == prompt


def test_guardrail_langgraph_node():
    """Test guardrail_node function integrated with LangGraph ComplianceState."""
    raw_prompt = "Update client Klara Schneider with email klara@web.de"
    state = create_initial_state(user_prompt=raw_prompt)

    update_dict = guardrail_node(state)

    assert "sanitized_prompt" in update_dict
    assert "pii_mapping" in update_dict
    assert "detected_pii" in update_dict
    assert "messages" in update_dict

    sanitized = update_dict["sanitized_prompt"]
    assert "klara@web.de" not in sanitized
    assert "Klara Schneider" not in sanitized
    assert len(update_dict["pii_mapping"]) >= 2
    assert len(update_dict["messages"]) == 1
    assert "[Guardrail Agent]" in update_dict["messages"][0].content
