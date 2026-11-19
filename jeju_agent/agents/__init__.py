from .access import AccessAgent
from .base import AgentContext, BaseAgent
from .course import CourseAgent
from .explore import ExploreAgent
from .status import StatusAgent

REGISTRY: dict[str, type[BaseAgent]] = {
    AccessAgent.name: AccessAgent,
    CourseAgent.name: CourseAgent,
    StatusAgent.name: StatusAgent,
    ExploreAgent.name: ExploreAgent,
}

__all__ = ["AgentContext", "BaseAgent", "AccessAgent", "CourseAgent", "ExploreAgent", "StatusAgent", "REGISTRY"]
