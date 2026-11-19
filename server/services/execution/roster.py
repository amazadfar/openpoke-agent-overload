"""Simple agent roster management - just a list of agent names."""

import json
import fcntl
import time
from pathlib import Path

from ...logging_config import logger


class AgentRoster:
    """Simple roster that stores agent names in a JSON file."""

    def __init__(self, roster_path: Path):
        self._roster_path = roster_path
        self._agents: list[str] = []
        self.load()

    def load(self) -> None:
        """Load agent names from roster.json."""
        if self._roster_path.exists():
            try:
                with open(self._roster_path, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._agents = [str(name) for name in data]
            except Exception as exc:
                logger.warning(f"Failed to load roster.json: {exc}")
                self._agents = []
        else:
            self._agents = []
            self.save()

    def save(self) -> None:
        """Save agent names to roster.json with file locking."""
        max_retries = 5
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                self._roster_path.parent.mkdir(parents=True, exist_ok=True)

                # Open file and acquire exclusive lock
                with open(self._roster_path, 'w') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        json.dump(self._agents, f, indent=2)
                        return
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            except BlockingIOError:
                # Lock is held by another process
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.warning("Failed to acquire lock on roster.json after retries")
            except Exception as exc:
                logger.warning(f"Failed to save roster.json: {exc}")
                break

    def add_agent(self, agent_name: str) -> None:
        """Add an agent to the roster if not already present."""
        if agent_name not in self._agents:
            self._agents.append(agent_name)
            self.save()

    def get_agents(self) -> list[str]:
        """Get list of all agent names."""
        return list(self._agents)

    def clear(self) -> None:
        """Clear the agent roster."""
        self._agents = []
        try:
            if self._roster_path.exists():
                self._roster_path.unlink()
            logger.info("Cleared agent roster")
        except Exception as exc:
            logger.warning(f"Failed to clear roster.json: {exc}")


from ...paths import data_dir

_DATA_DIR = data_dir()  # OPENPOKE_DATA_DIR override; default unchanged (server/data)
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"

_agent_roster = AgentRoster(_ROSTER_PATH)


class RosterFacade:
    """Same methods as AgentRoster (task 2.1 compatibility).

    AGENT_REGISTRY off: pure pass-through, roster.json is authoritative.
    AGENT_REGISTRY on: backed by the registry (canonical names only) and
    dual-writing canonical names to roster.json, so turning the flag off again
    leaves a consistent roster.
    """

    def __init__(self, roster: AgentRoster) -> None:
        self._roster = roster

    @staticmethod
    def _registry():
        from ... import flags

        if not flags.enabled("AGENT_REGISTRY"):
            return None
        from ..agents.registry import get_agent_registry

        return get_agent_registry()

    def load(self) -> None:
        self._roster.load()
        registry = self._registry()
        if registry is not None:
            registry.migrate_if_needed()
            registry.sync_from_roster(self._roster.get_agents())

    def save(self) -> None:
        self._roster.save()

    def get_agents(self) -> list[str]:
        registry = self._registry()
        if registry is None:
            return self._roster.get_agents()
        return [row.name for row in registry.list_all()]

    def add_agent(self, agent_name: str) -> None:
        registry = self._registry()
        if registry is None:
            self._roster.add_agent(agent_name)
            return
        row, _ = registry.create(agent_name)
        self._roster.add_agent(row.name)

    def clear(self) -> None:
        self._roster.clear()
        registry = self._registry()
        if registry is not None:
            registry.clear()


_roster_facade = RosterFacade(_agent_roster)


def get_agent_roster() -> RosterFacade:
    """Get the singleton roster (a facade over roster.json and, with AGENT_REGISTRY, the registry)."""
    return _roster_facade
