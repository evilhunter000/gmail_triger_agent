from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from config import Config
from email_parser import EmailData


@dataclass
class RuleResult:
    score: float              # -1.0 (strong spam) to +1.0 (strong important)
    category_hint: str        # 'Important' | 'Promotional' | 'Normal'
    signals_matched: List[str]
    is_decisive: bool         # if True, skip LLM and use this result directly
    whitelist_match: bool
    blacklist_match: bool
    high_risk_domain: bool    # .edu, .gov, bank — extra-conservative archiving


# ------------------------------------------------------------------
# Decisive thresholds
# ------------------------------------------------------------------

IMPORTANT_DECISIVE = 0.70   # score >= this → Important, skip LLM
SPAM_DECISIVE = -0.70       # score <= this → Promotional, skip LLM (not high-risk)

# High-risk domains require score <= -0.90 to be decisive Promotional
SPAM_DECISIVE_HIGH_RISK = -0.90


# ------------------------------------------------------------------
# Domain patterns
# ------------------------------------------------------------------

_HIGH_RISK_DOMAIN_PATTERNS = [
    re.compile(r"\.edu$", re.I),
    re.compile(r"\.gov$", re.I),
    re.compile(r"(irs|ssa|dhs|uscis|cbp|ice|state|hhs)\.gov$", re.I),
    re.compile(r"(chase|wellsfargo|bankofamerica|citi|citibank|schwab|fidelity|vanguard|tdbank|usbank|capitalone|pnc|regions|suntrust|truist)\.com$", re.I),
    re.compile(r"(paypal|stripe|venmo|zelle|cashapp)\.com$", re.I),
]

_IMPORTANT_DOMAIN_PATTERNS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"\.edu$", re.I), 0.35, "educational institution domain (.edu)"),
    (re.compile(r"\.gov$", re.I), 0.40, "government domain (.gov)"),
    (re.compile(r"(uscis|ssa|irs|dhs|cbp|ice)\.gov$", re.I), 0.50, "US government agency"),
    (re.compile(r"(chase|wellsfargo|bankofamerica|citi|citibank|schwab|fidelity|vanguard|tdbank|usbank|capitalone|pnc|regions|suntrust|truist)\.com$", re.I), 0.35, "major bank/financial institution"),
    (re.compile(r"(paypal|stripe|venmo)\.com$", re.I), 0.25, "payment platform"),
    (re.compile(r"(linkedin|greenhouse|lever|workday|taleo|icims|jobvite|ashby|smartrecruiters)\.com$", re.I), 0.20, "recruiting/job platform"),
    (re.compile(r"(docusign|hellosign|echosign)\.com$", re.I), 0.30, "document signing platform"),
    (re.compile(r"(zoom|calendly|cal\.com)\.com$", re.I), 0.15, "meeting/calendar platform"),
]

_SPAM_DOMAIN_PATTERNS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"(shopify|bigcommerce|squarespace|woocommerce)\.com$", re.I), -0.20, "e-commerce platform"),
    (re.compile(r"(mailchimp|sendgrid|klaviyo|constantcontact|hubspot|marketo|braze|iterable|sailthru)\.com$", re.I), -0.30, "marketing email platform"),
]


# ------------------------------------------------------------------
# Keyword signals (pattern, weight, label)
# Subject is scored at 1.0x weight; body at 0.5x weight
# ------------------------------------------------------------------

_IMPORTANT_KEYWORDS: List[Tuple[re.Pattern, float, str]] = [
    # Immigration / visa
    (re.compile(r"\b(uscis|sevis|i-?20|opt|cpt|visa|f-?1|h-?1b|green card|immigration|i-?94|eads?|employment authorization)\b", re.I), 0.45, "immigration/visa terms"),
    # Job / career
    (re.compile(r"\b(interview|offer letter|job offer|internship offer|assessment|technical screen|onsite|virtual interview|recruiter|hiring manager)\b", re.I), 0.35, "job/interview terms"),
    (re.compile(r"\b(application (status|update|received|reviewed)|background check|reference check|start date|compensation|salary|signing bonus)\b", re.I), 0.30, "application/offer terms"),
    # Academic
    (re.compile(r"\b(professor|advisor|registrar|financial aid|bursar|transcript|gpa|grade|graduation|commencement|enrollment|tuition|scholarship|fellowship|thesis|dissertation)\b", re.I), 0.30, "academic terms"),
    (re.compile(r"\b(assignment|homework|midterm|final exam|quiz|course registration|class (drop|add|withdraw)|waitlist)\b", re.I), 0.25, "academic coursework"),
    # Action required
    (re.compile(r"\b(action required|response required|reply (required|needed|requested)|please (respond|reply|confirm|review|complete|sign|submit|verify|update|renew|pay))\b", re.I), 0.35, "action required"),
    (re.compile(r"\b(deadline|due (date|by|on)|expires? (on|at|by)|last day|by (monday|tuesday|wednesday|thursday|friday|today|tonight|tomorrow|end of (day|week|month)))\b", re.I), 0.30, "deadline mentioned"),
    # Finance / billing
    (re.compile(r"\b(invoice|bill(ing)?|payment (due|received|failed|declined|confirmation)|statement|account (balance|overdue|past due)|subscription (renewal|cancelled)|refund)\b", re.I), 0.30, "financial/billing terms"),
    (re.compile(r"\b(wire transfer|bank transfer|direct deposit|routing number|account number|tax form|w-?2|1099|i-?9)\b", re.I), 0.35, "sensitive financial terms"),
    # Security
    (re.compile(r"\b(password reset|login attempt|unusual (sign-?in|activity|access)|account (locked|suspended|compromised|breach)|verify your (account|identity|email|phone)|two.?factor|security (alert|code|key|notice))\b", re.I), 0.40, "security alert"),
    # Medical / legal / housing
    (re.compile(r"\b(appointment (confirmed|scheduled|reminder)|test results?|prescription|insurance (claim|denial|approval)|medical record)\b", re.I), 0.30, "medical/health terms"),
    (re.compile(r"\b(lease (agreement|renewal|termination)|eviction|court (date|notice|order|hearing)|subpoena|legal notice|attorney|lawsuit)\b", re.I), 0.35, "legal/housing terms"),
    # Personal contact signals
    (re.compile(r"\bhi (there |dear )?([\w]+)[,!]", re.I), 0.10, "personal greeting"),
    (re.compile(r"\bthank you for your (application|interview|interest|time|meeting)\b", re.I), 0.20, "formal acknowledgment"),
]

_SPAM_KEYWORDS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bunsubscribe\b", re.I), -0.30, "unsubscribe link/text"),
    (re.compile(r"\b(\d+%\s*off|percent off|save \$?\d+|savings of|discount(ed)?|promo code|coupon|voucher)\b", re.I), -0.28, "discount/coupon offer"),
    (re.compile(r"\b(sale|clearance|flash sale|mega sale|super sale|blowout|door buster|going fast)\b", re.I), -0.25, "sale language"),
    (re.compile(r"\b(limited time( offer)?|act now|today only|ends (today|tonight|soon)|while supplies last|don.?t miss out|hurry|last chance)\b", re.I), -0.28, "urgency/scarcity marketing"),
    (re.compile(r"\b(shop now|buy now|order now|click here|get yours?|grab yours?|claim (your|it|now)|redeem (now|your|offer))\b", re.I), -0.22, "marketing call-to-action"),
    (re.compile(r"\b(newsletter|weekly (digest|roundup|picks|deals)|monthly (update|picks|newsletter)|exclusive (deals?|offers?|access))\b", re.I), -0.25, "newsletter/digest format"),
    (re.compile(r"\b(deals?|hot deals?|best deals?|top deals?|handpicked|curated (for you|picks?)|recommended for you|because you (shopped|browsed|bought|viewed))\b", re.I), -0.22, "personalized marketing"),
    (re.compile(r"\b(free shipping|free delivery|ships free|no minimum( order)?)\b", re.I), -0.18, "free shipping offer"),
    (re.compile(r"\b(win(ner)?|prize|reward|gift card|sweepstakes|giveaway|lucky (winner|draw)|you.?ve (been selected|won))\b", re.I), -0.25, "prize/giveaway language"),
    (re.compile(r"\b(subscribe|follow us|join us|become a member|sign up (for|to) (our|the|receive))\b", re.I), -0.15, "subscription solicitation"),
    (re.compile(r"\b(new (arrivals?|collection|products?|styles?|drops?|season)|just launched|now available|introducing)\b", re.I), -0.18, "product launch announcement"),
    (re.compile(r"\b(no longer (want|wish|like)|manage (email )?preferences|opt out|remove me)\b", re.I), -0.20, "email preference management"),
]


# ------------------------------------------------------------------
# Header-level spam signals
# ------------------------------------------------------------------

def _score_headers(email: EmailData) -> Tuple[float, List[str]]:
    score = 0.0
    signals: List[str] = []

    if email.is_bulk_sender:
        score -= 0.30
        signals.append("bulk sender header (List-ID or Precedence: bulk)")

    if email.has_unsubscribe_link:
        # Slightly less than keyword match to avoid double-counting
        score -= 0.15
        signals.append("List-Unsubscribe header present")

    if email.reply_to_differs:
        score -= 0.15
        signals.append("Reply-To domain differs from sender domain")

    return score, signals


# ------------------------------------------------------------------
# Main rule engine
# ------------------------------------------------------------------

class RuleEngine:
    def __init__(self, config: Config) -> None:
        self._whitelist_domains = set(config.whitelist_domains)
        self._blacklist_domains = set(config.blacklist_domains)
        self._whitelist_senders = set(config.whitelist_senders)

    def score(self, email: EmailData) -> RuleResult:
        signals: List[str] = []

        # 1. Whitelist / blacklist overrides
        if email.from_email in self._whitelist_senders or email.from_domain in self._whitelist_domains:
            return RuleResult(
                score=1.0,
                category_hint="Important",
                signals_matched=["sender/domain in whitelist"],
                is_decisive=True,
                whitelist_match=True,
                blacklist_match=False,
                high_risk_domain=True,
            )

        blacklisted = email.from_domain in self._blacklist_domains
        if blacklisted:
            signals.append("sender domain in blacklist")

        # 2. Detect high-risk domain
        high_risk = _is_high_risk_domain(email.from_domain)

        # 3. Domain pattern scoring
        domain_score, domain_signals = _score_domain(email.from_domain)
        signals.extend(domain_signals)

        # 4. Keyword scoring (subject at 1.0x, body at 0.5x)
        subject_text = email.subject + " " + email.snippet
        body_text = email.body_text

        important_subj, imp_subj_signals = _score_keywords(subject_text, _IMPORTANT_KEYWORDS, weight=1.0)
        important_body, imp_body_signals = _score_keywords(body_text, _IMPORTANT_KEYWORDS, weight=0.5)
        spam_subj, spam_subj_signals = _score_keywords(subject_text, _SPAM_KEYWORDS, weight=1.0)
        spam_body, spam_body_signals = _score_keywords(body_text, _SPAM_KEYWORDS, weight=0.5)

        keyword_score = important_subj + important_body + spam_subj + spam_body
        signals.extend(imp_subj_signals)
        signals.extend(set(imp_body_signals) - set(imp_subj_signals))  # avoid dupes
        signals.extend(spam_subj_signals)
        signals.extend(set(spam_body_signals) - set(spam_subj_signals))

        # 5. Header signals
        header_score, header_signals = _score_headers(email)
        signals.extend(header_signals)

        # 6. Combine and clamp
        raw_score = domain_score + keyword_score + header_score
        if blacklisted:
            raw_score -= 0.50

        final_score = max(-1.0, min(1.0, raw_score))

        # 7. Determine decisiveness
        spam_decisive_threshold = SPAM_DECISIVE_HIGH_RISK if high_risk else SPAM_DECISIVE
        is_decisive = (
            final_score >= IMPORTANT_DECISIVE
            or final_score <= spam_decisive_threshold
        )

        # 8. Determine category hint
        if final_score >= 0.20:
            category_hint = "Important"
        elif final_score <= -0.20:
            category_hint = "Promotional"
        else:
            category_hint = "Normal"

        return RuleResult(
            score=round(final_score, 4),
            category_hint=category_hint,
            signals_matched=list(dict.fromkeys(signals)),  # dedupe, preserve order
            is_decisive=is_decisive,
            whitelist_match=False,
            blacklist_match=blacklisted,
            high_risk_domain=high_risk,
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _is_high_risk_domain(domain: str) -> bool:
    return any(p.search(domain) for p in _HIGH_RISK_DOMAIN_PATTERNS)


def _score_domain(domain: str) -> Tuple[float, List[str]]:
    score = 0.0
    signals: List[str] = []
    for pattern, weight, label in _IMPORTANT_DOMAIN_PATTERNS:
        if pattern.search(domain):
            score += weight
            signals.append(label)
            break  # only first match counts per domain direction
    for pattern, weight, label in _SPAM_DOMAIN_PATTERNS:
        if pattern.search(domain):
            score += weight  # weight is negative
            signals.append(label)
            break
    return score, signals


def _score_keywords(
    text: str,
    patterns: List[Tuple[re.Pattern, float, str]],
    weight: float = 1.0,
) -> Tuple[float, List[str]]:
    if not text:
        return 0.0, []
    score = 0.0
    signals: List[str] = []
    for pattern, base_weight, label in patterns:
        if pattern.search(text):
            score += base_weight * weight
            signals.append(label)
    return score, signals
