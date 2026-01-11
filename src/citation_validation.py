"""Citation validation functions for the Deep Research agent."""

import asyncio
import logging
import re
from typing import List, Optional, Tuple

import aiohttp
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from config import Configuration
from state import CitationValidationResult, SourceRegistry, SourceStatus
from utils import get_api_key_for_model

logger = logging.getLogger(__name__)


async def validate_source_existence(
    content: str,
    source_registry: SourceRegistry
) -> Tuple[bool, List[str]]:
    """
    Verify all citations in content reference sources that exist in the registry.

    Supports both formats:
    - Markdown hyperlinks: [Title](URL)
    - Legacy numeric citations: [X]

    Args:
        content: The generated text (compressed research or final report)
        source_registry: Registry of all discovered sources

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []

    if not source_registry or not source_registry.sources:
        # No sources registered - can't validate
        return True, ["Warning: No sources registered for validation"]

    # Pattern: Markdown hyperlinks [Title](URL)
    markdown_link_pattern = r'\[([^\]]+)\]\((https?://[^\s\)]+)\)'
    markdown_links = re.findall(markdown_link_pattern, content)

    # Validate markdown hyperlink URLs exist in registry
    for title, url in markdown_links:
        # Clean URL of trailing punctuation
        url = url.rstrip('.,;:')
        normalized_url = url.rstrip('/')

        if normalized_url not in source_registry.url_to_source_id:
            # Check for partial matches (URL might be truncated or have query params)
            partial_match = any(
                registered_url.startswith(normalized_url) or
                normalized_url.startswith(registered_url)
                for registered_url in source_registry.url_to_source_id.keys()
            )
            if not partial_match:
                errors.append(f"Hyperlink URL not found in research data: [{title}]({url[:60]}...)")

    # Also support legacy numeric citation format [X] for backwards compatibility
    # Exclude numbers that are part of markdown links like [1](url)
    # by checking they're not followed by (
    numeric_only_pattern = r'\[(\d+)\](?!\()'
    numeric_citations = set(int(m) for m in re.findall(numeric_only_pattern, content))

    # Check if citation numbers exceed registered sources
    max_registered = len(source_registry.sources)
    for citation_num in numeric_citations:
        # If citation_map exists, check against it
        if source_registry.citation_map:
            if citation_num not in source_registry.citation_map:
                errors.append(f"Citation [{citation_num}] references non-existent source")
        else:
            # Otherwise check against total source count
            if citation_num > max_registered:
                errors.append(f"Citation [{citation_num}] exceeds registered sources ({max_registered})")

    return len(errors) == 0, errors


async def validate_citation_completeness(
    content: str,
    config: RunnableConfig
) -> Tuple[bool, List[str]]:
    """
    Check that all factual claims have citations using LLM analysis.

    Args:
        content: The generated report text
        config: Runtime configuration for model access

    Returns:
        Tuple of (is_valid, list of uncited claims)
    """
    configurable = Configuration.from_runnable_config(config)

    # Use summarization model (lighter) for validation
    validation_model = init_chat_model(
        model=configurable.summarization_model,
        max_tokens=2000,
        api_key=get_api_key_for_model(configurable.summarization_model, config),
        tags=["langsmith:nostream"]
    )

    # Limit content to prevent token overflow
    content_truncated = content[:15000] if len(content) > 15000 else content

    validation_prompt = f"""Analyze this research report for uncited factual claims.

Report:
{content_truncated}

Instructions:
1. Identify specific factual claims (statistics, dates, quotes, financial figures, specific events)
2. Check if each claim has a citation - either a markdown hyperlink [Title](URL) or legacy [X] format nearby
3. Return ONLY claims that clearly lack citations and would benefit from one

Output format (JSON):
{{
    "uncited_claims": [
        "Specific claim text without citation",
        ...
    ],
    "severity": "high" | "medium" | "low" | "none"
}}

If all important factual claims are cited, return: {{"uncited_claims": [], "severity": "none"}}
Only report significant uncited claims, not general statements or opinions.
"""

    try:
        response = await validation_model.ainvoke([HumanMessage(content=validation_prompt)])
        import json
        # Try to extract JSON from response
        response_text = response.content
        # Handle potential markdown code blocks
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]

        result = json.loads(response_text.strip())
        uncited = result.get("uncited_claims", [])
        return len(uncited) == 0, uncited
    except Exception as e:
        logger.warning(f"Citation completeness validation failed: {e}")
        # If validation fails, return warning but don't block
        return True, [f"Warning: Could not validate completeness: {str(e)}"]


def validate_citation_format(content: str) -> Tuple[bool, List[str]]:
    """
    Validate citation format consistency.

    Supports both formats:
    - Markdown hyperlinks: [Title](URL)
    - Legacy numeric citations: [X]

    Checks:
    - For markdown links: All hyperlinks in body have corresponding entry in Sources section
    - For legacy format: Sequential numbering without gaps [1], [2], [3]...
    - Sources section exists if citations are present

    Args:
        content: The report text with citations

    Returns:
        Tuple of (is_valid, list of format errors)
    """
    errors = []

    # Find Sources section (try multiple formats)
    sources_markers = ["### Sources", "## Sources", "# Sources", "**Sources**", "Sources:"]
    sources_section = ""
    body_text = content

    for marker in sources_markers:
        if marker in content:
            parts = content.split(marker, 1)
            body_text = parts[0]
            sources_section = parts[1] if len(parts) > 1 else ""
            break

    # Pattern: Markdown hyperlinks [Title](URL)
    markdown_link_pattern = r'\[([^\]]+)\]\((https?://[^\s\)]+)\)'
    body_markdown_links = re.findall(markdown_link_pattern, body_text)

    # Pattern: Legacy numeric citations [X] (not followed by parenthesis)
    numeric_only_pattern = r'\[(\d+)\](?!\()'
    body_numeric_citations = set(int(m) for m in re.findall(numeric_only_pattern, body_text))

    # Determine which format is being used
    has_markdown_links = len(body_markdown_links) > 0
    has_numeric_citations = len(body_numeric_citations) > 0

    if not has_markdown_links and not has_numeric_citations:
        # No citations found - might be okay for short reports
        return True, []

    # Check for Sources section
    if not sources_section:
        errors.append("Citations found but no Sources section present")
        return False, errors

    # Validate based on format used
    if has_markdown_links:
        # For markdown links, check that URLs in body appear in Sources section
        sources_markdown_links = re.findall(markdown_link_pattern, sources_section)
        sources_urls = set(url.rstrip('/') for _, url in sources_markdown_links)
        body_urls = set(url.rstrip('/') for _, url in body_markdown_links)

        # Check for URLs used in body but not in Sources
        missing_in_sources = body_urls - sources_urls
        if missing_in_sources and len(missing_in_sources) <= 5:
            errors.append(f"URLs cited in body but not in Sources section: {len(missing_in_sources)} missing")

    if has_numeric_citations:
        # Legacy format validation
        source_citations = set()
        source_line_pattern = r'^\s*\[(\d+)\]'
        for line in sources_section.split('\n'):
            match = re.match(source_line_pattern, line.strip())
            if match:
                source_citations.add(int(match.group(1)))

        # Check for sequential numbering (no gaps)
        if body_numeric_citations:
            max_citation = max(body_numeric_citations)
            expected = set(range(1, max_citation + 1))
            missing = expected - body_numeric_citations
            if missing and len(missing) <= 5:
                errors.append(f"Gap in citation sequence: missing {sorted(missing)}")

        # Check that all body citations have source entries
        undefined = body_numeric_citations - source_citations
        if undefined:
            errors.append(f"Citations used but not defined in Sources: {sorted(undefined)}")

    return len(errors) == 0, errors


async def validate_url_accessibility(
    source_registry: SourceRegistry,
    timeout: float = 10.0,
    max_concurrent: int = 5
) -> Tuple[bool, List[str]]:
    """
    Verify that cited URLs are accessible (return 2xx status).

    Args:
        source_registry: Registry containing all sources
        timeout: Request timeout in seconds
        max_concurrent: Maximum concurrent requests

    Returns:
        Tuple of (all_accessible, list of inaccessible URLs)
    """
    if not source_registry or not source_registry.sources:
        return True, []

    errors = []
    semaphore = asyncio.Semaphore(max_concurrent)

    async def check_url(source) -> Optional[str]:
        """Check single URL accessibility."""
        async with semaphore:
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.head(
                        source.url,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        allow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"}
                    ) as response:
                        if response.status >= 400:
                            source.status = SourceStatus.UNREACHABLE
                            return f"URL returned {response.status}: {source.url[:60]}..."
                        source.status = SourceStatus.VERIFIED
                        return None
            except asyncio.TimeoutError:
                source.status = SourceStatus.TIMEOUT
                return f"URL timeout: {source.url[:60]}..."
            except Exception as e:
                source.status = SourceStatus.UNREACHABLE
                return f"URL error ({type(e).__name__}): {source.url[:60]}..."

    # Only check URLs that are actually cited (in citation_map) or all if no map
    if source_registry.citation_map:
        sources_to_check = [
            source_registry.sources[sid]
            for sid in source_registry.citation_map.values()
            if sid in source_registry.sources
        ]
    else:
        sources_to_check = list(source_registry.sources.values())

    if not sources_to_check:
        return True, []

    results = await asyncio.gather(*[check_url(s) for s in sources_to_check])
    errors = [r for r in results if r is not None]

    return len(errors) == 0, errors


async def validate_citations(
    content: str,
    source_registry: Optional[SourceRegistry],
    config: RunnableConfig,
    check_url_accessibility: bool = False,
    check_completeness: bool = True
) -> CitationValidationResult:
    """
    Run all citation validations and return comprehensive result.

    Args:
        content: Generated content to validate
        source_registry: Registry of all sources
        config: Runtime configuration
        check_url_accessibility: Whether to verify URL accessibility (can be slow)
        check_completeness: Whether to check citation completeness (uses LLM)

    Returns:
        CitationValidationResult with all validation outcomes
    """
    result = CitationValidationResult(is_valid=True)

    # Initialize registry if None
    if source_registry is None:
        source_registry = SourceRegistry()
        result.warnings.append("No source registry provided - limited validation possible")

    # 1. Source existence validation
    try:
        exists_valid, exist_errors = await validate_source_existence(content, source_registry)
        result.source_existence_errors = exist_errors
    except Exception as e:
        logger.warning(f"Source existence validation failed: {e}")
        result.warnings.append(f"Source existence check failed: {str(e)}")

    # 2. Format validation
    try:
        format_valid, format_errors = validate_citation_format(content)
        result.format_errors = format_errors
    except Exception as e:
        logger.warning(f"Format validation failed: {e}")
        result.warnings.append(f"Format validation failed: {str(e)}")

    # 3. Citation completeness validation (optional, uses LLM)
    if check_completeness:
        try:
            complete_valid, complete_errors = await validate_citation_completeness(content, config)
            result.completeness_errors = complete_errors
        except Exception as e:
            logger.warning(f"Completeness validation failed: {e}")
            result.warnings.append(f"Completeness check failed: {str(e)}")

    # 4. URL accessibility (optional, can be slow)
    if check_url_accessibility:
        try:
            configurable = Configuration.from_runnable_config(config)
            url_valid, url_errors = await validate_url_accessibility(
                source_registry,
                timeout=configurable.citation_validation_timeout
            )
            result.url_accessibility_errors = url_errors
        except Exception as e:
            logger.warning(f"URL accessibility validation failed: {e}")
            result.warnings.append(f"URL accessibility check failed: {str(e)}")

    # Calculate overall validity
    # Source existence and format are critical errors
    # Completeness is a warning (LLM-based, may have false positives)
    result.is_valid = (
        len(result.source_existence_errors) == 0 and
        len(result.format_errors) == 0
    )

    return result


def generate_citation_fix_prompt(
    final_report: str,
    validation_result: CitationValidationResult,
    source_registry: Optional[SourceRegistry]
) -> str:
    """
    Generate a prompt for fixing citation issues.

    Args:
        final_report: The report with citation issues
        validation_result: Validation result with errors
        source_registry: Registry of available sources

    Returns:
        Prompt string for LLM to fix citations
    """
    fix_instructions = []

    if validation_result.source_existence_errors:
        errors_list = "\n".join(f"- {e}" for e in validation_result.source_existence_errors[:10])
        fix_instructions.append(f"SOURCE EXISTENCE ERRORS (CRITICAL - must fix):\n{errors_list}")

    if validation_result.format_errors:
        errors_list = "\n".join(f"- {e}" for e in validation_result.format_errors)
        fix_instructions.append(f"FORMAT ERRORS (CRITICAL - must fix):\n{errors_list}")

    if validation_result.url_accessibility_errors:
        errors_list = "\n".join(f"- {e}" for e in validation_result.url_accessibility_errors[:5])
        fix_instructions.append(f"URL ACCESSIBILITY WARNINGS:\n{errors_list}")

    available_sources = source_registry.to_citation_list() if source_registry else "No sources available"

    return f"""You are fixing citation issues in a research report.

CURRENT REPORT:
{final_report}

VALIDATION ERRORS TO FIX:
{chr(10).join(fix_instructions)}

AVAILABLE SOURCES (these are the ONLY valid sources you can cite):
{available_sources}

INSTRUCTIONS:
1. Remove or replace any citations that reference non-existent sources
2. Use markdown hyperlinks for all citations: [Source Title](URL)
3. Each citation should be a clickable link directly in the text
4. Ensure all citations in the body have corresponding entries in the Sources section
5. Do NOT invent new sources - only use sources from the AVAILABLE SOURCES list
6. If a claim cannot be properly cited, either remove it or clearly mark it as unverified
7. Maintain the overall structure and content of the report
8. Keep the Sources section at the end as a list of markdown hyperlinks:
   - [Source Title 1](URL1)
   - [Source Title 2](URL2)

Output the corrected report in full."""
