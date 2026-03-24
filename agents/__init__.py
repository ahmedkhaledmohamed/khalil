"""Sub-agent delegation for parallel task execution."""

from agents.pool import delegate, fan_out, fan_out_named

__all__ = ["delegate", "fan_out", "fan_out_named"]
