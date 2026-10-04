"""
Code shared by the GROW2 AgentCore agents.

- common.sources        connectors to external grant databases (the *domain* layer)
- common.domain_config  loader for the domain config pack (config/domains/<domain>/)

Agents import this as `common.*`. In the container `common/` sits next to
`agent.py` under /app; locally `bc/invoke-local.sh` puts `bc/` on PYTHONPATH.
"""
