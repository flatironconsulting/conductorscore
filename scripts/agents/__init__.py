"""Agent adapters (ports-and-adapters edges).

Each agent gets a ``scripts/agents/<id>/`` package implementing the
``AgentAdapter`` port from ``scripts.agents.base``. ``scripts.agents.registry``
maps the ``CONDUCTORSCORE_PROVIDERS`` selection to concrete adapter instances.
"""
