"""Provider-neutral research contracts: fixture evidence by default, bounded live page research on request."""

from .live_source import (
    LiveResearchPolicy,
    LiveResearchRefused,
    LiveResearchUnavailable,
    PublicWebsiteResearchSource,
    permitted_public_target,
)
from .research_agent import FixtureResearchSource, ResearchAgent, ResearchJobResult

__all__ = [
    "FixtureResearchSource",
    "LiveResearchPolicy",
    "LiveResearchRefused",
    "LiveResearchUnavailable",
    "PublicWebsiteResearchSource",
    "ResearchAgent",
    "ResearchJobResult",
    "permitted_public_target",
]
