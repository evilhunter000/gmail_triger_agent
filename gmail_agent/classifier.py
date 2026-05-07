from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import anthropic

from config import Config
from email_parser import EmailData
from rules import RuleResult


# ------------------------------------------------------------------
# System prompt (stable → prompt caching on every call)
# ------------------------------------------------------------------

CLASSIFICATION_SYSTEM_PROMPT = """You are an email classifier for a personal Gmail triage agent.

Your task is to classify incoming emails into exactly one of three categories:
- Important: requires user attention, action, or awareness
- Promotional: marketing, advertising, newsletters, sale offers, cold outreach
- Normal: routine informational emails, receipts, FYI updates with no urgency

IMPORTANT CLASSIFICATION GUIDELINES:

Classify as Important if:
- Email is direct personal communication (addressed to user by name)
- University/academic: professors, assignments, grades, registration, tuition, scholarships
- Immigration/USCIS/visa: I-20, OPT, CPT, SEVIS, visa status, appointments, government notices
- Employment: interviews, job offers, rejections, recruiter outreach, assessments, background checks
- Financial: bank alerts, payment failures, invoice due, tax documents, wire transfers
- Security: password resets, login alerts, account verification, suspicious activity
- Government/legal: court notices, official government correspondence, legal documents
- Medical: appointments, test results, insurance decisions
- Emails with deadlines, required responses, approvals needed, or urgent action items
- Emails from known personal contacts requiring response

Classify as Promotional if:
- Sale offers, discounts, coupons, clearance, flash sales
- "Limited time" urgency language designed to drive purchases
- Newsletters with no direct personal relevance
- Product recommendations or advertisements
- Marketing campaigns from retail/e-commerce brands
- Cold sales outreach with no personal connection
- Subscription renewal reminders for non-critical services

Classify as Normal if:
- Order confirmations and shipping notifications (already paid, no action needed)
- FYI announcements that do not require response
- Automated system notifications (non-security)
- Non-urgent account activity summaries
- Social media notifications (likes, follows, comments)
- App update notifications

CRITICAL SAFETY RULES (apply these first, before other rules):
- If sender domain is .edu or .gov: require 0.90+ confidence before Promotional; when in doubt → Important
- If email contains immigration, USCIS, visa, or I-20 terms: classify as Important even if it also looks like a newsletter
- If email contains security terms (password reset, login attempt, account alert): classify as Important
- If financial domain (major bank, PayPal, Stripe) AND urgent language: Important, not Promotional
- When genuinely uncertain: classify as Normal (leaving in inbox is always safer than archiving)

Output ONLY valid JSON in this exact format, no other text:
{
  "category": "Important" | "Promotional" | "Normal",
  "confidence": 0.0-1.0,
  "reason": "1-2 sentence explanation of why this classification was chosen",
  "suggested_action": "reply" | "review" | "pay" | "submit" | "attend" | "no_action"
}"""


@dataclass
class LLMResult:
    category: str          # 'Important' | 'Promotional' | 'Normal'
    confidence: float      # 0.0 - 1.0
    reason: str
    suggested_action: str
    cache_hit: bool        # True if system prompt was served from cache


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    reason: str
    suggested_action: str
    rule_score: float
    llm_confidence: Optional[float]
    used_llm: bool
    cache_hit: bool


class Classifier:
    def __init__(self, config: Config) -> None:
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._model = config.claude_model

    def classify(self, email: EmailData) -> LLMResult:
        user_prompt = _build_user_prompt(email)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=[
                    {
                        "type": "text",
                        "text": CLASSIFICATION_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIError as exc:
            raise RuntimeError(f"Claude API error: {exc}") from exc

        raw_text = response.content[0].text if response.content else ""

        # Detect cache hit
        usage = response.usage
        cache_hit = bool(getattr(usage, "cache_read_input_tokens", 0))

        return _parse_llm_response(raw_text, cache_hit)


# ------------------------------------------------------------------
# Score combination
# ------------------------------------------------------------------

def combine_scores(
    rule_result: RuleResult,
    llm_result: Optional[LLMResult],
    important_threshold: float,
    spam_threshold: float,
) -> ClassificationResult:
    """Merge rule engine and LLM outputs into a final classification."""

    # Whitelist / blacklist overrides — no LLM needed
    if rule_result.whitelist_match:
        return ClassificationResult(
            category="Important",
            confidence=1.0,
            reason="Sender is in the whitelist — automatically classified as Important.",
            suggested_action="review",
            rule_score=rule_result.score,
            llm_confidence=None,
            used_llm=False,
            cache_hit=False,
        )

    if rule_result.blacklist_match and not rule_result.high_risk_domain:
        return ClassificationResult(
            category="Promotional",
            confidence=1.0,
            reason="Sender domain is in the blacklist.",
            suggested_action="no_action",
            rule_score=rule_result.score,
            llm_confidence=None,
            used_llm=False,
            cache_hit=False,
        )

    # Decisive rule result — skip LLM
    if rule_result.is_decisive and llm_result is None:
        rule_abs = abs(rule_result.score)
        category = rule_result.category_hint
        reason = "Rule-based classification: " + ", ".join(rule_result.signals_matched[:3])
        return ClassificationResult(
            category=category,
            confidence=round(rule_abs, 4),
            reason=reason,
            suggested_action="review" if category == "Important" else "no_action",
            rule_score=rule_result.score,
            llm_confidence=None,
            used_llm=False,
            cache_hit=False,
        )

    # LLM was called — combine scores
    if llm_result is None:
        # Fallback: LLM failed, use rules conservatively
        return ClassificationResult(
            category="Normal",
            confidence=0.50,
            reason="LLM classification unavailable; leaving in inbox for safety.",
            suggested_action="no_action",
            rule_score=rule_result.score,
            llm_confidence=None,
            used_llm=False,
            cache_hit=False,
        )

    rule_cat = rule_result.category_hint
    llm_cat = llm_result.category
    llm_conf = llm_result.confidence
    rule_abs = abs(rule_result.score)

    # Map rule score [-1, 1] to a rough confidence 0→1
    rule_conf = (rule_result.score + 1.0) / 2.0 if rule_cat == "Important" else (1.0 - rule_result.score) / 2.0

    if rule_cat == llm_cat:
        # Agreement: average + small bonus
        combined_conf = min(1.0, (rule_conf + llm_conf) / 2.0 + 0.05)
        reason = llm_result.reason
    else:
        delta = abs(rule_conf - llm_conf)
        if delta > 0.30:
            # One signal clearly dominates — use the more confident one
            if llm_conf >= rule_conf:
                combined_conf = llm_conf * 0.90  # slight penalty for disagreement
                llm_cat, reason = llm_cat, llm_result.reason
            else:
                llm_cat = rule_cat
                combined_conf = rule_conf * 0.90
                reason = "Rule-based: " + ", ".join(rule_result.signals_matched[:2])
        else:
            # Genuine uncertainty — be conservative
            llm_cat = "Normal"
            combined_conf = max(rule_abs, llm_conf) * 0.65
            reason = f"Mixed signals — rules suggest {rule_cat}, LLM suggests {llm_result.category}. Leaving in inbox."

    # Safety override: high-risk domain + Promotional → require very high confidence
    if llm_cat == "Promotional" and rule_result.high_risk_domain:
        if combined_conf < 0.90:
            llm_cat = "Normal"
            reason = f"High-risk sender domain: lowering confidence below archive threshold. Original: {reason}"
            combined_conf *= 0.70

    return ClassificationResult(
        category=llm_cat,
        confidence=round(combined_conf, 4),
        reason=reason,
        suggested_action=llm_result.suggested_action,
        rule_score=rule_result.score,
        llm_confidence=llm_conf,
        used_llm=True,
        cache_hit=llm_result.cache_hit,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _build_user_prompt(email: EmailData) -> str:
    lines = [
        f"From: {email.from_addr}",
        f"Sender domain: {email.from_domain}",
        f"Subject: {email.subject}",
        f"Date: {email.date}",
        f"Has unsubscribe link: {'yes' if email.has_unsubscribe_link else 'no'}",
        f"Is bulk/list sender: {'yes' if email.is_bulk_sender else 'no'}",
        f"Reply-To differs from From: {'yes' if email.reply_to_differs else 'no'}",
        f"Is part of ongoing thread: {'yes' if email.is_thread_reply else 'no'}",
    ]
    if email.has_attachments:
        lines.append(f"Attachments: {', '.join(email.attachment_names)}")

    body = email.body_text or email.snippet or "(no body)"
    lines += [
        "",
        "Email body (first 2000 chars):",
        "---",
        body[:2000],
        "---",
        "",
        "Classify this email.",
    ]
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID_CATEGORIES = {"Important", "Promotional", "Normal"}
_VALID_ACTIONS = {"reply", "review", "pay", "submit", "attend", "no_action"}


def _parse_llm_response(text: str, cache_hit: bool) -> LLMResult:
    match = _JSON_RE.search(text)
    if not match:
        return _uncertain_result(cache_hit, "LLM response was not valid JSON")

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return _uncertain_result(cache_hit, "LLM response JSON parse failed")

    category = data.get("category", "Normal")
    if category not in _VALID_CATEGORIES:
        category = "Normal"

    try:
        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    reason = str(data.get("reason", ""))[:300]
    action = data.get("suggested_action", "no_action")
    if action not in _VALID_ACTIONS:
        action = "no_action"

    return LLMResult(
        category=category,
        confidence=confidence,
        reason=reason,
        suggested_action=action,
        cache_hit=cache_hit,
    )


def _uncertain_result(cache_hit: bool, reason: str) -> LLMResult:
    return LLMResult(
        category="Normal",
        confidence=0.0,
        reason=reason,
        suggested_action="no_action",
        cache_hit=cache_hit,
    )
