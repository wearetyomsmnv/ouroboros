"""
AI Security Research tool.

Searches for new publications in AI Security from arXiv, Papers with Code,
and major AI lab blogs. Optionally evaluates associated code repositories.

Tools exposed:
  - ai_security_search    : search recent AI security papers/posts
  - ai_security_eval_code : evaluate code quality/security for a given GitHub repo
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Well-known AI security / safety blogs with RSS feeds
_BLOG_FEEDS: Dict[str, str] = {
    "Anthropic": "https://www.anthropic.com/news/rss",
    "OpenAI": "https://openai.com/blog/rss.xml",
    "DeepMind": "https://deepmind.google/blog/rss.xml",
    "Alignment Forum": "https://www.alignmentforum.org/feed.xml",
    "LessWrong": "https://www.lesswrong.com/feed.xml?view=community&karmaThreshold=25",
    "AI Snake Oil": "https://www.aisnakeoil.com/feed",
}

# arXiv categories relevant to AI security
_ARXIV_CATEGORIES = ["cs.CR", "cs.AI", "cs.LG", "cs.CL"]

_AI_SECURITY_KEYWORDS = [
    "adversarial", "jailbreak", "prompt injection", "data poisoning",
    "backdoor", "trojan", "robustness", "alignment", "AI safety",
    "red team", "hallucination", "deceptive alignment", "misalignment",
    "LLM security", "model extraction", "membership inference",
    "differential privacy", "watermark", "machine unlearning",
    "gradient leakage", "inversion attack", "evasion attack",
    "certified defense", "model stealing",
]

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: int = 15) -> str:
    """Simple HTTP GET — returns response body as string."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Ouroboros-AI-Security/1.0 (research tool; non-commercial)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# arXiv search
# ---------------------------------------------------------------------------


def _build_arxiv_query(keywords: List[str], categories: List[str]) -> str:
    kw_parts = [f'ti:"{kw}" OR abs:"{kw}"' for kw in keywords[:8]]
    cat_parts = [f"cat:{c}" for c in categories]
    return f"({' OR '.join(kw_parts)}) AND ({' OR '.join(cat_parts)})"


def _search_arxiv(query: str, max_results: int = 10, days_back: int = 7) -> List[Dict]:
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    try:
        xml_text = _http_get(f"{_ARXIV_API}?{params}")
    except Exception as e:
        log.warning("arXiv fetch failed: %s", e)
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("arXiv XML parse error: %s", e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    results = []

    for entry in root.findall("atom:entry", _ARXIV_NS):
        try:
            title = (entry.findtext("atom:title", "", _ARXIV_NS) or "").strip().replace("\n", " ")
            summary = (entry.findtext("atom:summary", "", _ARXIV_NS) or "").strip().replace("\n", " ")
            published_str = (entry.findtext("atom:published", "", _ARXIV_NS) or "").strip()
            arxiv_id = (entry.findtext("atom:id", "", _ARXIV_NS) or "").strip()

            pub_dt = None
            if published_str:
                try:
                    pub_dt = datetime.fromisoformat(published_str.rstrip("Z")).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            if pub_dt and pub_dt < cutoff:
                continue

            authors = [
                (a.findtext("atom:name", "", _ARXIV_NS) or "").strip()
                for a in entry.findall("atom:author", _ARXIV_NS)
            ]
            cats = [c.get("term", "") for c in entry.findall("atom:category", _ARXIV_NS)]
            gh_links = list(set(re.findall(r"https?://github\.com/[^\s\"'>)]+", summary)))

            results.append({
                "source": "arXiv",
                "title": title,
                "url": arxiv_id,
                "published": published_str[:10],
                "authors": authors[:5],
                "categories": [c for c in cats if c],
                "summary": summary[:500] + ("..." if len(summary) > 500 else ""),
                "github_links": gh_links,
            })
        except Exception as e:
            log.debug("Error parsing arXiv entry: %s", e)

    return results


# ---------------------------------------------------------------------------
# Blog RSS search
# ---------------------------------------------------------------------------


def _parse_rss_feed(source: str, url: str, keywords: List[str], days_back: int) -> List[Dict]:
    try:
        xml_text = _http_get(url, timeout=10)
    except Exception as e:
        log.debug("Feed %s fetch failed: %s", source, e)
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    kw_lower = [k.lower() for k in keywords]
    items = []

    # RSS 2.0
    for item in root.iter("item"):
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            pub_date_str = (item.findtext("pubDate") or "").strip()

            combined = (title + " " + desc).lower()
            if not any(kw in combined for kw in kw_lower):
                continue

            pub_dt = None
            if pub_date_str:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_date_str)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            if pub_dt and pub_dt < cutoff:
                continue

            desc_clean = re.sub(r"<[^>]+>", "", desc)[:400]
            gh_links = re.findall(r"https?://github\.com/[^\s\"'>)]+", desc)

            items.append({
                "source": source,
                "title": title,
                "url": link,
                "published": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
                "summary": desc_clean,
                "github_links": gh_links,
            })
        except Exception:
            continue

    # Atom 1.0 fallback
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            try:
                title_el = entry.find("atom:title", ns)
                link_el = entry.find("atom:link", ns)
                content_el = entry.find("atom:content", ns)
                summary_el = entry.find("atom:summary", ns)
                pub_el = entry.find("atom:published", ns) or entry.find("atom:updated", ns)

                title = (title_el.text or "").strip() if title_el is not None else ""
                link = (link_el.get("href", "") if link_el is not None else "")
                desc = ""
                if content_el is not None and content_el.text:
                    desc = content_el.text.strip()
                elif summary_el is not None and summary_el.text:
                    desc = summary_el.text.strip()

                combined = (title + " " + desc).lower()
                if not any(kw in combined for kw in kw_lower):
                    continue

                pub_dt = None
                pub_str = (pub_el.text or "").strip() if pub_el is not None else ""
                if pub_str:
                    try:
                        pub_dt = datetime.fromisoformat(pub_str.rstrip("Z")).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

                if pub_dt and pub_dt < cutoff:
                    continue

                desc_clean = re.sub(r"<[^>]+>", "", desc)[:400]
                gh_links = re.findall(r"https?://github\.com/[^\s\"'>)]+", desc)

                items.append({
                    "source": source,
                    "title": title,
                    "url": link,
                    "published": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
                    "summary": desc_clean,
                    "github_links": gh_links,
                })
            except Exception:
                continue

    return items


# ---------------------------------------------------------------------------
# Code evaluation — security patterns
# ---------------------------------------------------------------------------

# (pattern, message, severity)
_SECURITY_PATTERNS = [
    (r"pickle\.loads?\s*\(", "pickle deserialization — arbitrary code execution risk", "HIGH"),
    (r"torch\.load\s*\([^)]*\)", "torch.load without weights_only=True — code execution risk", "HIGH"),
    (r"eval\s*\(", "eval() usage — potential code injection", "HIGH"),
    (r"exec\s*\(", "exec() usage — potential code injection", "HIGH"),
    (r"yaml\.load\s*\([^)]*(?!Loader\s*=\s*yaml\.SafeLoader)", "yaml.load (unsafe) — use yaml.safe_load", "HIGH"),
    (r"os\.system\s*\(", "os.system() — shell injection risk", "MEDIUM"),
    (r"subprocess\.[a-z_]+\s*\([^)]*shell\s*=\s*True", "subprocess with shell=True", "MEDIUM"),
    (r"__import__\s*\(", "Dynamic __import__ call", "MEDIUM"),
    (r"requests\.[a-z]+\s*\([^)]*verify\s*=\s*False", "SSL verification disabled", "MEDIUM"),
    (r"hashlib\.(md5|sha1)\s*\(", "Weak hash (MD5/SHA1) — not for security use", "LOW"),
    (r"random\.(random|randint|choice)\s*\(", "Non-cryptographic RNG — not for security", "LOW"),
    (r"assert\s+", "assert statements — disabled with -O flag in production", "LOW"),
]

_QUALITY_PATTERNS = [
    (r"(password|passwd|secret|api_key|token|auth_key)\s*=\s*['\"][^'\"]{4,}['\"]",
     "Possible hardcoded credential"),
    (r"except\s*:", "Bare except clause — catches all exceptions silently"),
    (r"# (TODO|FIXME|HACK|XXX)\b", "Incomplete/hacky code marker"),
    (r"print\s*\(", "print() usage — prefer logging"),
    (r"\bglobal\s+\w+", "Global variable mutation"),
]

_POSITIVE_PATTERNS = [
    (r"torch\.manual_seed|np\.random\.seed|random\.seed", "Random seeds set ✓"),
    (r"\.github[/\\]workflows", "CI/CD workflows ✓"),
    (r"if\s+__name__\s*==\s*['\"]__main__['\"]", "Proper __main__ guard ✓"),
    (r"argparse|hydra|omegaconf|click", "Configurable CLI ✓"),
    (r"logging\.(getLogger|basicConfig)", "Proper logging ✓"),
    (r"@?pytest|unittest", "Tests present ✓"),
    (r"requirements\.txt|setup\.py|pyproject\.toml|environment\.yml|conda", "Dependency file ✓"),
    (r"README", "README present ✓"),
]


def _fetch_github_meta(repo_url: str) -> Optional[Dict]:
    m = re.match(r"https?://github\.com/([^/\s#?]+/[^/\s#?]+)", repo_url)
    if not m:
        return None
    slug = m.group(1).rstrip("/")

    meta: Dict = {}
    try:
        meta = json.loads(_http_get(f"https://api.github.com/repos/{slug}"))
    except Exception as e:
        log.debug("GitHub API meta failed for %s: %s", slug, e)

    branch = meta.get("default_branch", "main")

    files: List[str] = []
    try:
        tree = json.loads(_http_get(
            f"https://api.github.com/repos/{slug}/git/trees/{branch}?recursive=1"
        ))
        files = [i["path"] for i in tree.get("tree", []) if i.get("type") == "blob"]
    except Exception as e:
        log.debug("GitHub tree fetch failed for %s: %s", slug, e)

    return {
        "slug": slug,
        "description": meta.get("description", ""),
        "stars": meta.get("stargazers_count", 0),
        "language": meta.get("language", ""),
        "topics": meta.get("topics", []),
        "default_branch": branch,
        "files": files,
        "open_issues": meta.get("open_issues_count", 0),
        "license": (meta.get("license") or {}).get("spdx_id", "None"),
        "updated_at": meta.get("updated_at", ""),
    }


def _fetch_python_sample(slug: str, branch: str, files: List[str], max_files: int = 5) -> Dict[str, str]:
    py_files = [f for f in files if f.endswith(".py")][:max_files]
    contents = {}
    for path in py_files:
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/{path}"
        try:
            contents[path] = _http_get(url, timeout=10)[:12000]
        except Exception:
            pass
    return contents


def _analyze_code(file_contents: Dict[str, str], file_list: List[str]) -> Dict[str, Any]:
    all_code = "\n".join(file_contents.values())
    file_list_str = "\n".join(file_list)
    combined = all_code + "\n" + file_list_str

    security_issues = []
    for pattern, message, severity in _SECURITY_PATTERNS:
        hits = re.findall(pattern, all_code, re.IGNORECASE)
        if hits:
            security_issues.append({"severity": severity, "issue": message, "occurrences": len(hits)})

    quality_issues = []
    for pattern, message in _QUALITY_PATTERNS:
        hits = re.findall(pattern, all_code, re.IGNORECASE | re.MULTILINE)
        if hits:
            quality_issues.append({"issue": message, "occurrences": len(hits)})

    positive_signals = []
    for pattern, message in _POSITIVE_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            positive_signals.append(message)

    return {
        "files_scanned": list(file_contents.keys()),
        "lines_scanned": len(all_code.split("\n")),
        "security_issues": sorted(security_issues, key=lambda x: ["HIGH", "MEDIUM", "LOW"].index(x["severity"])),
        "quality_issues": quality_issues,
        "positive_signals": positive_signals,
    }


def _compute_score(analysis: Dict) -> Dict:
    score = 100
    breakdown = []

    penalties = {"HIGH": 20, "MEDIUM": 10, "LOW": 3}
    for issue in analysis["security_issues"]:
        p = penalties.get(issue["severity"], 5)
        score -= p
        breakdown.append(f"-{p}: {issue['issue']}")

    for issue in analysis["quality_issues"]:
        if "hardcoded credential" in issue["issue"].lower():
            score -= 15
            breakdown.append(f"-15: {issue['issue']}")

    # Bonuses
    for signal in analysis["positive_signals"]:
        score += 3
        breakdown.append(f"+3: {signal}")

    score = max(0, min(100, score))
    grade = (
        "A" if score >= 85 else
        "B" if score >= 70 else
        "C" if score >= 50 else
        "D" if score >= 30 else "F"
    )
    return {"score": score, "grade": grade, "breakdown": breakdown}


def _make_recommendations(analysis: Dict, scoring: Dict) -> List[str]:
    recs = []
    high = [i for i in analysis["security_issues"] if i["severity"] == "HIGH"]
    medium = [i for i in analysis["security_issues"] if i["severity"] == "MEDIUM"]

    if high:
        recs.append("🚨 Fix HIGH severity: " + "; ".join(i["issue"] for i in high))
    if medium:
        recs.append("⚠️  Fix MEDIUM severity: " + "; ".join(i["issue"] for i in medium))

    creds = [i for i in analysis["quality_issues"] if "hardcoded" in i["issue"].lower()]
    if creds:
        recs.append("🔑 Remove hardcoded credentials — use environment variables")

    if not any("seed" in s for s in analysis["positive_signals"]):
        recs.append("🎲 Set random seeds for reproducibility (torch.manual_seed, np.random.seed)")
    if not any("Dependency" in s for s in analysis["positive_signals"]):
        recs.append("📦 Add requirements.txt or pyproject.toml")
    if not any("Tests" in s for s in analysis["positive_signals"]):
        recs.append("🧪 Add tests (pytest/unittest)")
    if not any("logging" in s.lower() for s in analysis["positive_signals"]):
        recs.append("📝 Replace print() with proper logging")

    if not recs:
        recs.append("✅ No major issues found.")
    return recs


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _ai_security_search(
    ctx: ToolContext,
    query: str = "",
    days_back: int = 7,
    max_results: int = 10,
    sources: str = "all",
) -> str:
    source_list = [s.strip().lower() for s in sources.split(",")]
    use_arxiv = "all" in source_list or "arxiv" in source_list
    use_blogs = "all" in source_list or "blogs" in source_list

    keywords = ([query] + _AI_SECURITY_KEYWORDS) if query else _AI_SECURITY_KEYWORDS

    all_results: List[Dict] = []

    if use_arxiv:
        q = _build_arxiv_query(keywords[:6], _ARXIV_CATEGORIES)
        all_results.extend(_search_arxiv(q, max_results=max_results, days_back=days_back))

    if use_blogs:
        for name, feed_url in _BLOG_FEEDS.items():
            all_results.extend(_parse_rss_feed(name, feed_url, keywords, days_back)[:3])

    all_results.sort(key=lambda r: r.get("published", "") or "", reverse=True)

    if not all_results:
        return json.dumps({
            "status": "no_results",
            "message": f"No AI security publications found in the last {days_back} days.",
            "tip": "Try increasing days_back or using a different query.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "query": query or "(default AI security sweep)",
        "days_back": days_back,
        "total_found": len(all_results),
        "results": all_results[:max_results * 2],
    }, ensure_ascii=False, indent=2)


def _ai_security_eval_code(
    ctx: ToolContext,
    repo_url: str,
    deep: bool = False,
) -> str:
    meta = _fetch_github_meta(repo_url)
    if meta is None:
        return json.dumps({
            "status": "error",
            "message": f"Cannot parse GitHub URL: {repo_url}",
        }, ensure_ascii=False, indent=2)

    max_files = 15 if deep else 5
    file_contents = _fetch_python_sample(meta["slug"], meta["default_branch"], meta["files"], max_files)

    if not file_contents:
        return json.dumps({
            "status": "partial",
            "message": "No Python files fetched (repo may be private, empty, or non-Python).",
            "repo": {k: meta[k] for k in ("slug", "description", "stars", "language", "license")},
        }, ensure_ascii=False, indent=2)

    analysis = _analyze_code(file_contents, meta["files"])
    scoring = _compute_score(analysis)
    recs = _make_recommendations(analysis, scoring)

    return json.dumps({
        "status": "ok",
        "repo": {
            "slug": meta["slug"],
            "url": repo_url,
            "description": meta["description"],
            "stars": meta["stars"],
            "language": meta["language"],
            "license": meta["license"],
            "open_issues": meta["open_issues"],
            "last_updated": (meta["updated_at"] or "")[:10],
            "topics": meta["topics"],
            "total_files": len(meta["files"]),
            "python_files": len([f for f in meta["files"] if f.endswith(".py")]),
        },
        "security_score": scoring["score"],
        "grade": scoring["grade"],
        "score_breakdown": scoring["breakdown"],
        "security_issues": analysis["security_issues"],
        "quality_issues": analysis["quality_issues"],
        "positive_signals": analysis["positive_signals"],
        "files_scanned": analysis["files_scanned"],
        "lines_scanned": analysis["lines_scanned"],
        "recommendations": recs,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="ai_security_search",
            schema={
                "name": "ai_security_search",
                "description": (
                    "Search for recent AI security publications from arXiv and major AI lab blogs "
                    "(Anthropic, OpenAI, DeepMind, Alignment Forum, LessWrong, etc.). "
                    "Returns papers/posts with titles, summaries, dates, and GitHub links found. "
                    "Topics covered: adversarial attacks, jailbreaks, prompt injection, backdoors, "
                    "alignment, red-teaming, privacy attacks, model extraction, watermarking, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional extra search query. "
                                "E.g. 'prompt injection LLM' or 'adversarial training'. "
                                "Default: broad AI security sweep."
                            ),
                        },
                        "days_back": {
                            "type": "integer",
                            "description": "Days back to search (default: 7).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results per source (default: 10).",
                        },
                        "sources": {
                            "type": "string",
                            "description": "Sources: 'arxiv', 'blogs', or 'all' (default: 'all').",
                        },
                    },
                    "required": [],
                },
            },
            handler=_ai_security_search,
            timeout_sec=60,
        ),
        ToolEntry(
            name="ai_security_eval_code",
            schema={
                "name": "ai_security_eval_code",
                "description": (
                    "Evaluate a GitHub repository from an AI security paper or blog post. "
                    "Fetches repo metadata and Python source files, scans for: "
                    "security vulnerabilities (pickle, eval/exec, torch.load, yaml.load, "
                    "hardcoded secrets, unsafe subprocess, SSL bypass), "
                    "code quality issues, and reproducibility signals (seeds, tests, CI/CD). "
                    "Returns a security score 0-100, grade A-F, and actionable recommendations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo_url": {
                            "type": "string",
                            "description": "GitHub repository URL (https://github.com/owner/repo).",
                        },
                        "deep": {
                            "type": "boolean",
                            "description": "Scan up to 15 Python files instead of 5 (slower, more thorough).",
                        },
                    },
                    "required": ["repo_url"],
                },
            },
            handler=_ai_security_eval_code,
            timeout_sec=90,
        ),
    ]
