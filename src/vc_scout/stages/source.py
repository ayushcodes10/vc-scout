"""Stage 1 - source.

Discovers candidate startups from Hacker News and produces a shortlist that is defensible
on topic rather than on popularity.

The funnel, in order: run a bounded family of intent-focused searches; keep only links
that could be a company's own product surface; classify each story's topical relevance;
discard everything irrelevant or below the relevance threshold *before* ranking;
deduplicate by canonical domain; then fill the shortlist from directly relevant candidates
first, letting adjacent ones take only a bounded share.

Every rejection is counted and every individual failure recorded, so a short shortlist can
be explained from ``source-report.json`` alone. A malformed hit, an unparseable timestamp
or a failed query variant never fails the run, and the stage never pads the shortlist to
reach the requested limit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vc_scout.discovery import (
    MIN_RELEVANCE_SCORE,
    DiscoveryRank,
    RejectionReason,
    RelevanceClass,
    accept_product_url,
    canonical_website,
    discovery_rank,
    parse_story_title,
)
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.discovery import DISCOVERY_FORMULA_VERSION, ORDERING_POLICY
from vc_scout.models.enums import ClaimLabel, SourceKind, TractionKind
from vc_scout.models.report import DiscardedHit, SourceReport, VariantResult
from vc_scout.models.source import SourceReference, TractionSignal
from vc_scout.net.hn import (
    HnAlgoliaClient,
    HnError,
    HnStory,
    MalformedHitError,
    QueryVariant,
    parse_hit,
    query_variants,
)
from vc_scout.store import RunStore
from vc_scout.util.ids import company_id_for, normalize_url, registrable_domain
from vc_scout.util.jsonio import write_json

__all__ = ["ADJACENT_MAX_SHARE", "SourceOutcome", "run_source"]

#: How far back to look. Older launches say little about a company's current state.
DEFAULT_WINDOW_DAYS = 730

#: Overfetch factor. The funnel discards most of what it fetches - text posts, code hosts,
#: duplicate domains, off-topic launches - so it has to start much wider than the limit.
OVERFETCH_FACTOR = 3
MIN_HITS_PER_PAGE = 20
MAX_HITS_PER_PAGE = 50

#: Ceiling on the share of the shortlist that merely-adjacent candidates may occupy while
#: directly relevant supply lasts. Once direct candidates run out, adjacent ones may fill
#: the remainder - a short list of weaker candidates beats a padded list of irrelevant ones.
ADJACENT_MAX_SHARE = 0.30


@dataclass(slots=True)
class SourceOutcome:
    """What the stage produced, for the CLI to summarise."""

    candidates: CandidateSet
    report: SourceReport
    candidates_path: str
    report_path: str


@dataclass(slots=True)
class _Discovered:
    """A story that survived URL acceptance, before relevance filtering."""

    story: HnStory
    domain: str
    launch_url: str
    website: str
    website_note: str | None
    name: str
    one_liner: str | None
    rank: DiscoveryRank
    variant_label: str

    @property
    def order_key(self) -> tuple[int, float, float, float, str]:
        """Lexicographic sort key, descending on every numeric term.

        Class first, then relevance score, then quality. Engagement lives inside
        ``quality_score`` and therefore cannot lift a candidate past a more relevant one.
        Ties fall back to the newer story, then to object ID, so ordering is total and
        stable.
        """
        return (
            -self.rank.class_rank,
            -self.rank.relevance_score,
            -self.rank.quality_score,
            -self.story.created_at.timestamp(),
            self.story.object_id,
        )


@dataclass(slots=True)
class _VariantAccumulator:
    """Mutable working state for one query variant, sealed into a ``VariantResult``."""

    variant: QueryVariant
    hits_returned: int = 0
    pages_fetched: int = 0
    raw_paths: list[str] = field(default_factory=list)
    error: str | None = None

    def seal(self) -> VariantResult:
        return VariantResult(
            label=self.variant.label,
            query=self.variant.query,
            tags=self.variant.tags,
            endpoint=self.variant.endpoint,
            weight=self.variant.weight,
            hits_returned=self.hits_returned,
            pages_fetched=self.pages_fetched,
            raw_paths=self.raw_paths,
            error=self.error,
        )


@dataclass(slots=True)
class _Funnel:
    """Counters and rejection records for the sourcing report."""

    counts: dict[str, int] = field(default_factory=dict)
    discarded: list[DiscardedHit] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def bump(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def discard(self, reason: str, **fields: str | None) -> None:
        self.bump(f"rejected_{reason}")
        self.discarded.append(DiscardedHit(reason=reason, **fields))


def _hits_per_page(limit: int) -> int:
    return min(MAX_HITS_PER_PAGE, max(MIN_HITS_PER_PAGE, limit * OVERFETCH_FACTOR))


def _traction_signals(story: HnStory, source_id: str) -> list[TractionSignal]:
    """Structured, source-backed signals taken straight from the story metadata.

    Points and comments are community reaction, not company claims, so they are labelled
    ``third_party``. Neither is treated as evidence of revenue.
    """
    return [
        TractionSignal(
            kind=TractionKind.HN_POINTS,
            value=str(story.points),
            numeric_value=float(story.points),
            label=ClaimLabel.THIRD_PARTY,
            source_ids=[source_id],
            observed_at=story.created_at,
        ),
        TractionSignal(
            kind=TractionKind.HN_COMMENTS,
            value=str(story.num_comments),
            numeric_value=float(story.num_comments),
            label=ClaimLabel.THIRD_PARTY,
            source_ids=[source_id],
            observed_at=story.created_at,
        ),
        TractionSignal(
            kind=TractionKind.LAUNCH_DATE,
            value=story.created_at.date().isoformat(),
            label=ClaimLabel.THIRD_PARTY,
            source_ids=[source_id],
            observed_at=story.created_at,
        ),
    ]


def _safe_object_id(hit: object) -> str | None:
    if isinstance(hit, dict):
        value = hit.get("objectID")
        if isinstance(value, str):
            return value
    return None


def _discover(story: HnStory, variant: QueryVariant, now: datetime) -> _Discovered:
    """Build the working record for an accepted story, including its relevance."""
    launch_url = normalize_url(story.url)
    website, website_note = canonical_website(story.url)
    name, one_liner = parse_story_title(story.title)
    domain = registrable_domain(launch_url)
    if not name:
        # No usable name in the title: fall back to the domain rather than inventing one.
        name, one_liner = domain, one_liner or story.title

    return _Discovered(
        story=story,
        domain=domain,
        launch_url=launch_url,
        website=website,
        website_note=website_note,
        name=name,
        one_liner=one_liner,
        rank=discovery_rank(
            title=story.title,
            one_liner=one_liner,
            url=launch_url,
            points=story.points,
            num_comments=story.num_comments,
            age_days=story.age_days(now),
            variant_weight=variant.weight,
        ),
        variant_label=variant.label,
    )


def _collect(
    client: HnAlgoliaClient,
    *,
    query: str,
    limit: int,
    now: datetime,
    window_days: int,
    store: RunStore,
    funnel: _Funnel,
) -> tuple[list[_Discovered], list[VariantResult]]:
    """Run every query variant, persist raw responses and accept usable stories."""
    since_unix = int((now - timedelta(days=window_days)).timestamp())
    hits_per_page = _hits_per_page(limit)

    variant_results: list[VariantResult] = []
    seen_stories: set[str] = set()
    discovered: list[_Discovered] = []

    for variant in query_variants(query):
        acc = _VariantAccumulator(variant=variant)
        try:
            body = client.search(variant, hits_per_page=hits_per_page, since_unix=since_unix)
        except HnError as exc:
            # One failed variant must not fail the run; the others still contribute.
            acc.error = str(exc)
            funnel.failures.append(f"query variant {variant.label!r} failed: {exc}")
            variant_results.append(acc.seal())
            continue

        raw_path = store.raw_hn_path(variant.label, page=0)
        write_json(raw_path, body)
        acc.raw_paths.append(store.relative(raw_path))
        acc.pages_fetched = 1

        hits = body.get("hits")
        if not isinstance(hits, list):
            acc.error = "response contained no hits array"
            funnel.failures.append(f"query variant {variant.label!r} returned no hits array")
            variant_results.append(acc.seal())
            continue

        acc.hits_returned = len(hits)
        funnel.bump("hits_fetched", len(hits))

        for hit in hits:
            try:
                story = parse_hit(hit)
            except MalformedHitError as exc:
                reason = (
                    RejectionReason.NO_URL
                    if "no external url" in str(exc)
                    else RejectionReason.MALFORMED
                )
                funnel.discard(reason, object_id=_safe_object_id(hit), detail=str(exc))
                continue

            if story.object_id in seen_stories:
                funnel.discard(
                    RejectionReason.DUPLICATE_STORY,
                    object_id=story.object_id,
                    title=story.title,
                )
                continue
            seen_stories.add(story.object_id)

            accepted, rejection = accept_product_url(story.url)
            if not accepted:
                funnel.discard(
                    rejection or RejectionReason.UNSAFE_URL,
                    object_id=story.object_id,
                    title=story.title,
                    url=story.url,
                )
                continue

            discovered.append(_discover(story, variant, now))

        variant_results.append(acc.seal())

    funnel.bump("unique_stories", len(seen_stories))
    funnel.bump("usable_product_urls", len(discovered))
    return discovered, variant_results


def _filter_eligible(discovered: list[_Discovered], funnel: _Funnel) -> list[_Discovered]:
    """Drop off-topic candidates before anything is ranked or truncated.

    This runs ahead of deduplication and truncation deliberately. Filtering after the cut
    would let a popular but irrelevant story occupy a shortlist slot and then be removed,
    leaving the shortlist shorter than it needed to be.
    """
    eligible: list[_Discovered] = []
    for item in discovered:
        if item.rank.relevance_class is RelevanceClass.IRRELEVANT:
            funnel.discard(
                RejectionReason.IRRELEVANT,
                object_id=item.story.object_id,
                title=item.story.title,
                url=item.launch_url,
                detail="no AI-automation signal in the title, one-liner or URL",
            )
            continue
        if item.rank.relevance_score < MIN_RELEVANCE_SCORE:
            funnel.discard(
                RejectionReason.BELOW_THRESHOLD,
                object_id=item.story.object_id,
                title=item.story.title,
                url=item.launch_url,
                detail=(
                    f"relevance {item.rank.relevance_score:.2f} is below the minimum "
                    f"{MIN_RELEVANCE_SCORE:.2f}"
                ),
            )
            continue
        eligible.append(item)
    return eligible


def _deduplicate_by_domain(items: list[_Discovered], funnel: _Funnel) -> list[_Discovered]:
    """Keep the highest-ranked story per canonical domain.

    A company that has launched on HN more than once produces several stories pointing at
    one domain. The survivor is chosen by the same lexicographic order used for the
    shortlist, so the retained story is the most relevant one, not merely the most popular.
    """
    best: dict[str, _Discovered] = {}
    for item in sorted(items, key=lambda d: d.order_key):
        existing = best.get(item.domain)
        if existing is None:
            best[item.domain] = item
            continue
        funnel.discard(
            RejectionReason.DUPLICATE_DOMAIN,
            object_id=item.story.object_id,
            title=item.story.title,
            url=item.launch_url,
            detail=f"domain {item.domain} already represented by story {existing.story.object_id}",
        )
    return sorted(best.values(), key=lambda d: d.order_key)


def _adjacent_allowance(*, direct_available: int, remaining: int, limit: int) -> tuple[int, bool]:
    """How many adjacent candidates may take a shortlist place.

    While enough directly relevant candidates exist to fill all but the adjacent share,
    adjacent ones are held to that share. Once direct supply falls below that, they may
    fill whatever is left.

    Returns ``(allowance, share_bound)``. ``share_bound`` records whether the share policy
    was the binding constraint, so a rejected adjacent candidate can be told it lost to the
    policy rather than merely to the limit. Note that under fill-direct-first the numeric
    cap is never the tighter of the two - it is stated explicitly anyway so the policy is
    visible and testable rather than an emergent side effect of the fill order.
    """
    share_cap = int(limit * ADJACENT_MAX_SHARE)
    share_bound = direct_available >= limit - share_cap
    if share_bound:
        return min(remaining, share_cap), True
    return remaining, False


def _compose_shortlist(
    eligible: list[_Discovered], *, limit: int, funnel: _Funnel
) -> list[_Discovered]:
    """Fill from directly relevant candidates first, then bounded adjacent ones.

    While direct supply lasts, adjacent candidates are held to ``ADJACENT_MAX_SHARE`` of
    the shortlist. Once it runs out they may fill the remainder - a shorter list of weaker
    candidates is still better than one padded with off-topic ones, and the shortfall is
    reported either way.
    """
    direct = [i for i in eligible if i.rank.relevance_class is RelevanceClass.DIRECT]
    adjacent = [i for i in eligible if i.rank.relevance_class is RelevanceClass.ADJACENT]

    taken_direct = direct[:limit]
    remaining = limit - len(taken_direct)

    allowance, share_bound = _adjacent_allowance(
        direct_available=len(direct), remaining=remaining, limit=limit
    )
    taken_adjacent = adjacent[:allowance]
    shortlist = sorted([*taken_direct, *taken_adjacent], key=lambda d: d.order_key)

    for item in direct[len(taken_direct) :]:
        funnel.discard(
            RejectionReason.OVER_LIMIT,
            object_id=item.story.object_id,
            title=item.story.title,
            url=item.launch_url,
            detail=f"ranked below the top {limit} directly relevant candidates",
        )
    dropped_adjacent = adjacent[len(taken_adjacent) :]
    if share_bound:
        reason = RejectionReason.ADJACENT_SHARE
        detail = (
            f"adjacent candidates are held to {int(ADJACENT_MAX_SHARE * 100)}% of a "
            f"{limit}-place shortlist while {len(direct)} direct candidates are available"
        )
    else:
        reason = RejectionReason.OVER_LIMIT
        detail = f"ranked below the top {limit} after direct candidates were exhausted"
    for item in dropped_adjacent:
        funnel.discard(
            reason,
            object_id=item.story.object_id,
            title=item.story.title,
            url=item.launch_url,
            detail=detail,
        )
    return shortlist


def _to_candidate(
    item: _Discovered, *, query: str, now: datetime
) -> tuple[Candidate, list[SourceReference]]:
    """Build a candidate with both of its sources.

    Two references are recorded: the Hacker News discussion, which carries the points,
    comments and launch date, and the launch URL exactly as posted. ``website`` may be the
    site origin rather than the launch URL, so keeping the launch URL as a source means the
    memo can still cite the page the claim actually came from.
    """
    hn_source = SourceReference.create(
        item.story.discussion_url,
        kind=SourceKind.HN_STORY,
        title=item.story.title,
        retrieved_at=now,
        published_at=item.story.created_at,
        hn_object_id=item.story.object_id,
        hn_points=item.story.points,
        hn_num_comments=item.story.num_comments,
    )
    launch_source = SourceReference.create(
        item.launch_url,
        kind=SourceKind.COMPANY_PAGE,
        title=item.story.title,
        retrieved_at=now,
        published_at=item.story.created_at,
    )

    notes = [
        f"Discovered via the {item.variant_label} query variant.",
        f"Relevance classified {item.rank.relevance_class.value} "
        f"({item.rank.relevance_score:.2f}) from matched terms "
        f"{ {group: terms for group, terms in item.rank.matched.items() if terms} }.",
    ]
    if item.website_note:
        notes.append(item.website_note)

    candidate = Candidate(
        company_id=company_id_for(item.name, item.website),
        name=item.name,
        source_ids=[hn_source.source_id, launch_source.source_id],
        one_liner=item.one_liner,
        website=item.website,
        discovered_via_query=query,
        discovered_at=now,
        discovery_rank=item.rank,
        traction_signals=_traction_signals(item.story, hn_source.source_id),
        notes=notes,
    )
    return candidate, [hn_source, launch_source]


def _class_counts(items: list[_Discovered]) -> dict[str, int]:
    counter = Counter(item.rank.relevance_class.value for item in items)
    return {cls.value: counter.get(cls.value, 0) for cls in RelevanceClass}


def run_source(
    *,
    store: RunStore,
    client: HnAlgoliaClient,
    query: str,
    limit: int,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> SourceOutcome:
    """Execute the sourcing stage and persist its artifacts."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    store.ensure_root()
    funnel = _Funnel()

    discovered, variant_results = _collect(
        client,
        query=query,
        limit=limit,
        now=now,
        window_days=window_days,
        store=store,
        funnel=funnel,
    )

    # Counted over everything that passed URL acceptance, so the report shows how many
    # were dropped as off-topic rather than only what survived.
    before = _class_counts(discovered)

    eligible = _deduplicate_by_domain(_filter_eligible(discovered, funnel), funnel)
    funnel.bump("unique_domains", len(eligible))
    eligible_counts = _class_counts(eligible)
    funnel.bump("eligible_direct", eligible_counts[RelevanceClass.DIRECT.value])
    funnel.bump("eligible_adjacent", eligible_counts[RelevanceClass.ADJACENT.value])

    shortlist = _compose_shortlist(eligible, limit=limit, funnel=funnel)

    candidates: list[Candidate] = []
    sources: dict[str, SourceReference] = {}
    seen_company_ids: set[str] = set()
    kept: list[_Discovered] = []
    for item in shortlist:
        try:
            candidate, refs = _to_candidate(item, query=query, now=now)
        except ValueError as exc:
            # A candidate that cannot be represented is dropped, never fatal.
            funnel.discard(
                RejectionReason.MALFORMED,
                object_id=item.story.object_id,
                title=item.story.title,
                url=item.launch_url,
                detail=str(exc),
            )
            continue
        if candidate.company_id in seen_company_ids:
            # Distinct domains can still slug to one company id; keep the first.
            funnel.discard(
                RejectionReason.DUPLICATE_DOMAIN,
                object_id=item.story.object_id,
                title=item.story.title,
                url=item.launch_url,
                detail=f"company id {candidate.company_id} already used",
            )
            continue
        seen_company_ids.add(candidate.company_id)
        candidates.append(candidate)
        kept.append(item)
        for ref in refs:
            sources.setdefault(ref.source_id, ref)

    after = _class_counts(kept)
    funnel.bump("candidates_kept", len(candidates))
    funnel.bump("candidates_direct", after[RelevanceClass.DIRECT.value])
    funnel.bump("candidates_adjacent", after[RelevanceClass.ADJACENT.value])

    shortfall = max(limit - len(candidates), 0)
    notes: list[str] = []
    if shortfall:
        notes.append(
            f"Kept {len(candidates)} candidates against a requested limit of {limit}: "
            f"{shortfall} short. Only {eligible_counts[RelevanceClass.DIRECT.value]} directly "
            f"relevant and {eligible_counts[RelevanceClass.ADJACENT.value]} adjacent candidates "
            "survived the relevance gate. The shortlist was not padded with off-topic results."
        )

    candidate_set = CandidateSet(
        run_id=store.run_id,
        query=query,
        candidates=candidates,
        sources=sorted(sources.values(), key=lambda s: s.source_id),
        requested_limit=limit,
        generated_at=now,
        notes=notes,
    )
    report = SourceReport(
        run_id=store.run_id,
        query=query,
        requested_limit=limit,
        generated_at=now,
        variants=variant_results,
        counts=dict(sorted(funnel.counts.items())),
        discarded=funnel.discarded,
        failures=funnel.failures,
        notes=notes,
        formula_version=DISCOVERY_FORMULA_VERSION,
        ordering_policy=ORDERING_POLICY,
        minimum_relevance=MIN_RELEVANCE_SCORE,
        relevance_before_selection=before,
        relevance_after_selection=after,
        shortfall=shortfall,
    )

    candidates_path = store.write_candidates(candidate_set)
    report_path = store.write_source_report(report)
    return SourceOutcome(
        candidates=candidate_set,
        report=report,
        candidates_path=store.relative(candidates_path),
        report_path=store.relative(report_path),
    )
