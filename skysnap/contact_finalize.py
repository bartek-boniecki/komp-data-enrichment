"""Reconcile LLM-extracted contacts with deterministic candidates + validation.

This is the last step of every enrichment: it normalizes phones to E.164,
validates emails (syntax + domain MX), prefers programmatically-found contacts
over LLM guesses when they are stronger, and as a last resort infers a likely
email from the person's name + a verified company domain.
"""

from __future__ import annotations

from urllib.parse import urlparse

from skysnap.contact_extract import ContactCandidate, ExtractedContacts
from skysnap.models import EnrichmentResult, WebsiteContact
from skysnap.validation import (
    domain_has_mx,
    email_domain,
    guess_email_patterns,
    infer_email_pattern,
    is_role_email,
    normalize_phone_pl,
    validate_email,
)

# B2B directory pages legitimately list the *target* company's phone/email, so a
# deterministic contact sourced from them is trusted even when the host differs
# from the company website. (News/press portals are deliberately excluded — a
# phone on a news article is the journalist's, not the company's.)
_DIRECTORY_HOSTS = (
    "panoramafirm.pl",
    "aleo.com",
    "pkt.pl",
    "gowork.pl",
    "rejestr.io",
    "krs-pobierz.pl",
)


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else f"//{url}").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host or None


def _domain_matches(host: str | None, domain: str) -> bool:
    if not host:
        return False
    domain = domain.lower()
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


def _is_directory_host(host: str | None) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in _DIRECTORY_HOSTS)


def _candidate_trusted(
    *, email_dom: str | None, source_url: str | None, restrict_domain: str | None
) -> bool:
    """Allowlist trust for a *deterministic* candidate.

    Trusted only when it clearly belongs to the target: on the company domain,
    or from a B2B directory page about the company. When the company domain is
    unknown we do NOT auto-promote deterministic candidates (they may belong to
    an unrelated org that merely co-occurred in search) — that judgement is left
    to the LLM.
    """
    host = _host_of(source_url)
    if restrict_domain:
        if email_dom and _domain_matches(email_dom, restrict_domain):
            return True
        if host and _domain_matches(host, restrict_domain):
            return True
        if host is None:  # e.g. JSON-LD parsed off the company page itself
            return True
    return _is_directory_host(host)


def _email_score(
    email: str,
    *,
    base: float,
    restrict_domain: str | None,
    check_mx: bool,
) -> float:
    verdict = validate_email(email, check_mx=check_mx)
    if not verdict["valid_syntax"]:
        return -1.0
    score = base
    if verdict["deliverable"]:
        score += 0.3
    if verdict["is_role"]:
        score -= 0.25
    if verdict["is_free_domain"]:
        score -= 0.2
    dom = email_domain(email)
    if restrict_domain and dom == restrict_domain.lower():
        score += 0.2
    return score


def _pick_best_email(
    candidates: list[tuple[str, float, bool]],
    *,
    restrict_domain: str | None,
    check_mx: bool,
) -> tuple[str, bool, bool] | None:
    """Return (email, is_role, is_direct_source) with the highest score, or None.

    ``is_direct_source`` records whether the winning candidate came from a
    field that claims to be the person's DIRECT channel — only those may
    populate ``direct_email`` downstream.
    """
    best: tuple[str, bool, bool] | None = None
    best_score = 0.0
    for email, base, is_direct in candidates:
        if not email:
            continue
        email = email.strip().lower()
        score = _email_score(email, base=base, restrict_domain=restrict_domain, check_mx=check_mx)
        if score < 0:
            continue
        if best is None or score > best_score:
            best = (email, is_role_email(email), bool(is_direct))
            best_score = score
    return best


def _normalize_phones(
    values: list[tuple[str | None, float, bool]],
) -> list[tuple[str, float, bool]]:
    out: list[tuple[str, float, bool]] = []
    seen: set[str] = set()
    for raw, base, is_direct in values:
        norm = normalize_phone_pl(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append((norm, base, bool(is_direct)))
    return out


def finalize_enrichment_contact(
    enrichment: EnrichmentResult,
    extracted: ExtractedContacts,
    *,
    restrict_domain: str | None = None,
    check_mx: bool = True,
    allow_pattern_guess: bool = True,
) -> EnrichmentResult:
    contact = enrichment.contact
    full_name = contact.full_name if contact else None
    note_extra: list[str] = []

    # ---- Emails: combine LLM + deterministic, pick the strongest ---------- #
    # When the company domain is known, only trust deterministic emails on that
    # domain (or from a directory page about the company) — otherwise we would
    # inject unrelated contacts that merely co-occurred in the search results.
    # Third tuple element = "claims to be the person's DIRECT channel".
    email_candidates: list[tuple[str, float, bool]] = []
    if contact and contact.email:
        email_candidates.append((contact.email, 0.55, False))
    if contact and contact.direct_email:
        email_candidates.append((contact.direct_email, 0.6, True))
    for c in extracted.emails:
        if not _candidate_trusted(
            email_dom=email_domain(c.value),
            source_url=c.source_url,
            restrict_domain=restrict_domain,
        ):
            continue
        email_candidates.append((c.value, c.score, False))

    chosen_email: str | None = None
    chosen_email_is_role = False
    chosen_email_is_direct = False
    best = _pick_best_email(email_candidates, restrict_domain=restrict_domain, check_mx=check_mx)
    if best:
        chosen_email, chosen_email_is_role, chosen_email_is_direct = best
    elif contact and (contact.email or contact.direct_email):
        # An email was present but every candidate failed validation — it must
        # be DROPPED, not exported. (Previously the invalid original survived
        # via an `or base_contact.email` fallback.)
        bad = contact.direct_email or contact.email
        note_extra.append(f"odrzucono niepoprawny email: {bad}")

    # ---- Pattern inference: name + verified domain, still no email -------- #
    # A guess is NOT a found contact: it goes into guessed_email only, never
    # into email/direct_email, and never raises confidence.
    guessed_email: str | None = None
    if not chosen_email and allow_pattern_guess and full_name:
        guess_domain = restrict_domain
        if not guess_domain and contact and contact.email:
            guess_domain = email_domain(contact.email)
        if guess_domain and (not check_mx or domain_has_mx(guess_domain)):
            # Learn the company's local-part convention from other personal
            # emails observed on the same domain (strongest possible prior).
            observed = [c.value for c in extracted.emails]
            company_pattern = infer_email_pattern(observed, guess_domain)
            for guess in guess_email_patterns(
                full_name, guess_domain, preferred_pattern=company_pattern
            ):
                guessed_email = guess
                basis = (
                    f"pattern {company_pattern} observed on domain"
                    if company_pattern
                    else "common PL corporate patterns"
                )
                note_extra.append(
                    f"NIEZWERYFIKOWANY email (zgadnięty z imienia+domeny): {guess}; {basis}"
                )
                break

    # ---- Phones: combine LLM + deterministic ----------------------------- #
    phone_inputs: list[tuple[str | None, float, bool]] = []
    if contact and contact.phone:
        phone_inputs.append((contact.phone, 0.55, False))
    if contact and contact.direct_phone:
        phone_inputs.append((contact.direct_phone, 0.6, True))
    for c in extracted.phones:
        if not _candidate_trusted(
            email_dom=None, source_url=c.source_url, restrict_domain=restrict_domain
        ):
            continue
        phone_inputs.append((c.value, c.score, False))
    phones = _normalize_phones(phone_inputs)
    chosen_phone: str | None = None
    chosen_phone_is_direct = False
    if phones:
        chosen_phone, _score, chosen_phone_is_direct = max(phones, key=lambda t: t[1])
    elif contact and (contact.phone or contact.direct_phone):
        bad_phone = contact.direct_phone or contact.phone
        note_extra.append(f"odrzucono niepoprawny telefon: {bad_phone}")

    if contact is None and chosen_email is None and chosen_phone is None and guessed_email is None:
        return enrichment  # nothing to add

    # ---- Assemble final contact ------------------------------------------ #
    # direct_* fields hold ONLY channels that came from a direct source. A
    # company switchboard picked up in `phone` must never surface as the
    # person's direct number.
    base_contact = contact or WebsiteContact(confidence=0.3)
    direct_email = (
        chosen_email
        if (chosen_email and chosen_email_is_direct and not chosen_email_is_role)
        else None
    )
    if direct_email is None and base_contact.direct_email:
        candidate = base_contact.direct_email.strip().lower()
        verdict = validate_email(candidate, check_mx=check_mx)
        if verdict["valid_syntax"] and not verdict["is_role"]:
            direct_email = candidate
    direct_phone = chosen_phone if (chosen_phone and chosen_phone_is_direct) else None
    if direct_phone is None and base_contact.direct_phone:
        direct_phone = normalize_phone_pl(base_contact.direct_phone)

    new_confidence = base_contact.confidence
    if chosen_email or chosen_phone:  # real finds only — a guess earns nothing
        new_confidence = max(new_confidence, 0.5)

    updated_contact = base_contact.model_copy(
        update={
            "email": chosen_email,
            "phone": chosen_phone,
            "direct_email": direct_email,
            "direct_phone": direct_phone,
            "guessed_email": guessed_email or base_contact.guessed_email,
            "confidence": new_confidence,
        }
    )

    updates: dict[str, object] = {"contact": updated_contact}
    if note_extra:
        existing_note = enrichment.notes or ""
        joined = " | ".join([n for n in (existing_note, *note_extra) if n.strip()])
        updates["notes"] = joined
    return enrichment.model_copy(update=updates)
